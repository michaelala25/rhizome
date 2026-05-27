"""BrowserView — top-level view for the new browser widget.

Two-region layout: the topic-tree panel on the left, the tab bar + active tab on the right. The
view is deliberately thin — it composes the panel and the tab area, owns the tab bar render and
``Ctrl+Left/Right`` cycling, and dispatches alt-arrow cross-region focus moves to whichever region
currently holds focus. Everything *inside* the panel (actions menu, tree, summary, rail expansion)
is the panel's business; everything *inside* the active tab is the tab's. Each side exposes a small
``nav_*`` / ``focus_*`` surface that ``BrowserView`` calls without knowing what's behind it.

Tab visibility is delegated to Textual's ``ContentSwitcher``: every tab view is mounted up front
(so first-visit latency is just the tab's own fetch, not widget construction), and switching tabs
flips the switcher's ``current`` to the right id. Combined with the VM's lazy filter propagation,
switching to a previously-visited tab that already matches the current filter is instantaneous;
switching to a stale or never-visited tab shows a "loading…" status while the tab fetches.

Tab-VM → tab-view mapping lives in ``_view_for_tab``: a flat dispatch on type, fine while we have
one concrete tab. When we add more, we'll either extend the dispatch table or have each tab VM
expose a ``make_view()`` factory.
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
    """Return a freshly-constructed view widget for ``tab_vm``.

    Dispatches on the VM's concrete type. Raises if the VM type has no registered view — that's a
    programmer error (we added a new tab VM without adding a view), and silently rendering a blank
    tab would hide it.
    """
    if isinstance(tab_vm, KnowledgeEntryBrowserTabViewModel):
        return KnowledgeEntryBrowserTabView(tab_vm)
    raise TypeError(
        f"No view registered for tab VM {type(tab_vm).__name__}. "
        f"Add a branch to _view_for_tab in browser/view.py."
    )


class BrowserView(Horizontal):
    """Top-level browser widget. Takes an externally-constructed ``BrowserViewModel`` and drives it
    through its mount lifecycle.

    The VM is owned by the caller (typically the chat tab appends a fresh ``BrowserViewModel`` to
    its feed; this view is built against that instance). ``await self._vm.start()`` runs from
    ``on_mount`` to do the actual DB work after Textual has finished mounting child widgets — by
    then everyone is subscribed to their VM's ``dirty`` and will repaint on first data arrival.

    Construction example::

        vm = BrowserViewModel(session_factory)
        view = BrowserView(vm)
        await container.mount(view)
    """

    # Fixed height for embedded use — when mounted inside the chat tab's ``VerticalScroll`` feed,
    # ``1fr`` resolves to 0 (the scroll container derives its content height from children, so a
    # child asking for "remaining" space has none to claim). 40 is the rough height of the legacy
    # ``/explore`` tab plus headroom for the summary panel; the user can tweak per-mount via CSS if
    # a different surface needs another size.
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
        # ``priority=True`` so these fire even when a deep descendant (e.g. a ``TextArea`` inside
        # the details panel) is focused — otherwise ``TextArea``'s own ``alt+left``/``alt+right``
        # word-navigation bindings would swallow the event and our region cycle would only work
        # when focus was on a non-editor widget. ``alt+up``/``alt+down`` are bound at the same
        # priority for the same reason — ``TextArea`` has its own paragraph-jump bindings on them.
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
        # Left: topic-tree panel — owns its own internal layout (title, actions, tree, summary),
        # CSS, expansion-on-focus, and internal cross-region nav. We just hand it the panel VM.
        yield TopicTreePanelView(self._vm.panel)

        # Right: tab bar over a ContentSwitcher that holds every tab view. The tab lineup is fixed
        # at ctor time (see BrowserViewModel), so we can mount every tab view up front and just
        # toggle which one is current.
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
        # Initial paint of the tab bar — the VM hasn't emitted dirty yet, so do it ourselves.
        self._refresh()
        # Now that all child widgets have mounted and subscribed to their respective VMs, kick off
        # the active tab's first fetch. The panel's child views load their own state on mount.
        await self._vm.start()
        # Give the tree focus by default so arrow keys work out of the gate.
        self._focus_tree()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    # Horizontal isn't focusable; route ``vm.request_focus()`` (fired by chat-tab feed nav) to the
    # tree inside the panel.
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
        """Render the tab bar as a Rich-styled text run: active tab is bold, inactives are dim.
        Cheap and avoids needing a real tab widget while the tab count is small."""
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
    # Two top-level regions: the topic-tree panel and the active tab. Within each, internal
    # navigation is delegated:
    #
    #   * ``panel.nav_left/nav_right`` move focus inside the panel (actions ↔ tree). They return
    #     False when at the leftmost / rightmost sub-region; ``False`` from ``nav_right`` means
    #     "advance into the next top-level region (the tab)". ``False`` from ``nav_left`` means
    #     "the panel is the leftmost top-level region, no-op".
    #   * ``tab.nav_left/nav_right/nav_up/nav_down`` handle navigation inside the active tab. The
    #     ``"topic_tree"`` sentinel from ``tab.nav_left`` means "I'm at my leftmost edge — focus
    #     should land back on the topic tree" (specifically the tree, not the actions menu).

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
        """Convenience: focus the topic tree inside the panel. Used at mount and from the
        VM-driven ``focus`` callback."""
        panel = self._panel_view()
        if panel is None:
            return
        panel.focus_tree()

    def _active_tab_view(self):
        """Return the currently-visible tab view widget, or ``None`` if the active index is out of
        range. Each tab view is mounted with id ``browser-tab-{i}`` so we look it up by id."""
        target_id = self._tab_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
