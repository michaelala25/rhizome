"""Top-level browser view. Composes the topic-tree panel on the left and the tab bar + active tab
on the right. Tab views are all mounted up front behind a ``ContentSwitcher`` so switching is just
a ``current = ...`` flip — first-visit latency is the tab's own fetch, not widget construction.

Alt-arrow navigation is bubble-up: each region (panel, tab) binds ``alt+arrow`` itself and walks
its own focus graph. This view only binds ``alt+left``/``alt+right`` as the cross-region
fall-through (panel → tab on right, tab → panel on left), which fire when a region's action
raises ``SkipAction`` because the step had no in-graph target. There's no cross-region ``alt+up``
or ``alt+down``, so those keys aren't bound here at all.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Static

from rhizome.logs import get_logger

from .knowledge_entry_tab import (
    KnowledgeEntryBrowserTabView,
    KnowledgeEntryBrowserTabViewModel,
)
from .tab_base import BrowserTabViewModel
from .topic_tree_panel import TopicTreePanelView
from .view_model import BrowserViewModel

_logger = get_logger("browser.view")


def _view_for_tab(tab_vm: BrowserTabViewModel):
    """Construct the view widget for ``tab_vm`` by dispatching on its concrete type. Raises rather
    than rendering a blank tab if a VM type has no registered view — that's a programmer error."""
    if isinstance(tab_vm, KnowledgeEntryBrowserTabViewModel):
        return KnowledgeEntryBrowserTabView(tab_vm)
    raise TypeError(
        f"No view registered for tab VM {type(tab_vm).__name__}. "
        f"Add a branch to _view_for_tab in browser/view.py."
    )


class BrowserView(Horizontal):
    """Takes a caller-constructed ``BrowserViewModel`` and drives it through its mount lifecycle.

    ``await self._vm.start()`` runs from ``on_mount`` after Textual finishes mounting children — by
    then every child widget is subscribed to its VM's ``dirty`` and will repaint on first arrival.
    """

    # Fixed height: inside the chat-tab ``VerticalScroll`` feed, ``1fr`` resolves to 0 (the scroll
    # container derives content height from children, so a child asking for "remaining" gets none).
    # 40 fits the body comfortably; callers can override per-mount via CSS.
    DEFAULT_CSS = """
    BrowserView {
        height: 40;
    }
    BrowserView #browser-right-tab {
        width: 1fr;
        height: 1fr;
        border: solid #3a3a3a;
    }
    BrowserView #browser-right-tab:focus-within {
        border: solid #6a6a6a;
    }
    BrowserView #browser-tab-bar {
        height: 1;
        padding: 0 1;
    }
    BrowserView #browser-tab-area {
        height: 1fr;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+right", "next_tab", show=False),
        Binding("ctrl+left", "prev_tab", show=False),
        # Cross-region fall-through only — fires when the focused region's own alt+arrow action
        # raised SkipAction (i.e., the step had no in-graph target). Up/down have no cross-region
        # meaning, so they're left for the regions alone.
        Binding("alt+right", "nav_right", show=False),
        Binding("alt+left", "nav_left", show=False),
    ]

    def __init__(self, view_model: BrowserViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    @property
    def view_model(self) -> BrowserViewModel:
        return self._vm

    def compose(self):
        yield TopicTreePanelView(self._vm.panel)
        with Vertical(id="browser-right-tab"):
            yield Static("", id="browser-tab-bar")
            initial_id = self._tab_widget_id(self._vm.active_index)
            with ContentSwitcher(initial=initial_id, id="browser-tab-area"):
                for index, tab_vm in enumerate(self._vm.tabs):
                    tab_view = _view_for_tab(tab_vm)
                    tab_view.id = self._tab_widget_id(index)
                    yield tab_view

    async def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.subscribe(self._vm.focus, self.focus)
        # The VM hasn't emitted dirty yet, so paint the tab bar once ourselves before fetching.
        self._refresh()
        await self._vm.start()
        self.focus()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    # ``Horizontal`` isn't focusable; route external focus requests through the panel (which in
    # turn lands focus on the topic tree via its own ``focus()`` override).
    def focus(self, scroll_visible: bool = True) -> "BrowserView":
        panel = self._panel_view()
        if panel is not None:
            panel.focus()
        return self

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._update_tab_bar()
        self._update_visible_tab()

    def _update_tab_bar(self) -> None:
        # Simple Rich text run: active tab bold, inactives dim. Cheap; promote to a real tab widget
        # if the tab count grows.
        tab_bar = self.query_one("#browser-tab-bar", Static)
        text = Text()
        for i, tab in enumerate(self._vm.tabs):
            if i > 0:
                text.append("   ")
            if i == self._vm.active_index:
                text.append(f" {tab.title} ", style="bold")
            else:
                text.append(f" {tab.title} ", style="dim")
        tab_bar.update(text)

    def _update_visible_tab(self) -> None:
        switcher = self.query_one("#browser-tab-area", ContentSwitcher)
        target = self._tab_widget_id(self._vm.active_index)
        if switcher.current != target:
            switcher.current = target

    @staticmethod
    def _tab_widget_id(index: int) -> str:
        return f"browser-tab-{index}"

    # ------------------------------------------------------------------
    # Key actions
    # ------------------------------------------------------------------

    def action_next_tab(self) -> None:
        self._vm.next_tab()

    def action_prev_tab(self) -> None:
        self._vm.prev_tab()

    # ------------------------------------------------------------------
    # Cross-region focus graph — two nodes (panel, tab), two edges (panel ↔ tab)
    # ------------------------------------------------------------------
    #
    # These actions only fire when the focused region's own ``alt+arrow`` action bubbled (raised
    # ``SkipAction``) because the step had no in-graph next step. Up/down have no cross-region
    # meaning, so they aren't bound here at all.

    def action_nav_left(self) -> None:
        if not self.nav_left():
            raise SkipAction()

    def action_nav_right(self) -> None:
        if not self.nav_right():
            raise SkipAction()

    def nav_left(self) -> bool:
        if self._focused_node() == "tab":
            return self._focus_node("panel")
        return False

    def nav_right(self) -> bool:
        if self._focused_node() == "panel":
            return self._focus_node("tab")
        return False

    # ------------------------------------------------------------------
    # Focus probes
    # ------------------------------------------------------------------

    def _focused_node(self) -> str | None:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return None
        panel = self._panel_view()
        if panel is not None and (focused is panel or panel in focused.ancestors_with_self):
            return "panel"
        tab = self._active_tab_view()
        if tab is not None and (focused is tab or tab in focused.ancestors_with_self):
            return "tab"
        return None

    def _focus_node(self, node: str) -> bool:
        """Focus the named region. Each region's own ``focus()`` override routes to the right
        inner widget (panel → topic tree, tab → entries table)."""
        target = self._panel_view() if node == "panel" else self._active_tab_view()
        if target is None:
            return False
        target.focus()
        return True

    def _panel_view(self) -> TopicTreePanelView | None:
        try:
            return self.query_one(TopicTreePanelView)
        except Exception:
            return None

    def _active_tab_view(self):
        target_id = self._tab_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
