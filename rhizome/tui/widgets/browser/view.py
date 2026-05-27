"""Top-level browser view. Composes the topic-tree panel on the left and the tab bar + active tab
on the right; dispatches the alt-arrow cross-region focus walk to whichever side currently has
focus. Each side exposes a small ``nav_*`` / ``focus_*`` surface this view calls without reaching
in. Tab views are all mounted up front behind a ``ContentSwitcher`` so switching is just a
``current = ...`` flip — first-visit latency is the tab's own fetch, not widget construction.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

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
    # 40 fits the body + summary; callers can override per-mount via CSS.
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
        # ``priority=True`` so these fire even when a descendant ``TextArea`` is focused — its own
        # word-nav (alt+←/→) and paragraph-jump (alt+↑/↓) bindings would otherwise swallow the event.
        Binding("alt+right", "focus_right", priority=True, show=False),
        Binding("alt+left", "focus_left", priority=True, show=False),
        Binding("alt+up", "focus_up", priority=True, show=False),
        Binding("alt+down", "focus_down", priority=True, show=False),
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
        self._focus_tree()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    # ``Horizontal`` isn't focusable; route external focus requests to the tree inside the panel.
    def focus(self, scroll_visible: bool = True) -> "BrowserView":
        self._focus_tree(scroll_visible=scroll_visible)
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
    # Cross-region focus navigation (alt+arrow)
    # ------------------------------------------------------------------
    #
    # Two regions: panel (left) and active tab (right). Internal navigation is delegated to each
    # side's ``nav_*``. A ``False`` from ``panel.nav_right`` means "I'm at my rightmost — advance
    # into the tab"; ``False`` from ``panel.nav_left`` means "leftmost region, no-op". The tab's
    # ``nav_left`` can return the sentinel ``"topic_tree"`` to ask us to focus the tree.

    def action_focus_right(self) -> None:
        if self._focus_is_in_panel():
            panel = self._panel_view()
            if panel is None or not panel.nav_right():
                tab = self._active_tab_view()
                if tab is not None and hasattr(tab, "focus_first"):
                    tab.focus_first()
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_right"):
            tab.nav_right()

    def action_focus_left(self) -> None:
        if self._focus_is_in_panel():
            panel = self._panel_view()
            if panel is not None:
                panel.nav_left()
            return
        tab = self._active_tab_view()
        result = tab.nav_left() if tab is not None and hasattr(tab, "nav_left") else False
        if result == "topic_tree":
            panel = self._panel_view()
            if panel is not None:
                panel.focus_tree()

    def action_focus_up(self) -> None:
        if self._focus_is_in_panel():
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_up"):
            tab.nav_up()

    def action_focus_down(self) -> None:
        if self._focus_is_in_panel():
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_down"):
            tab.nav_down()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _focus_is_in_panel(self) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        panel = self._panel_view()
        if panel is None:
            return False
        return focused is panel or panel in focused.ancestors_with_self

    def _panel_view(self) -> TopicTreePanelView | None:
        try:
            return self.query_one(TopicTreePanelView)
        except Exception:
            return None

    def _focus_tree(self, *, scroll_visible: bool = True) -> None:
        panel = self._panel_view()
        if panel is None:
            return
        panel.focus_tree()

    def _active_tab_view(self):
        target_id = self._tab_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
