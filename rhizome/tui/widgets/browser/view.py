"""BrowserView — top-level view for the new browser widget.

Horizontal layout: topic tree on the left, tab bar + active tab on the right. Bootstraps the
``BrowserViewModel`` on mount and lets the VM drive which tab is visible.

Tab visibility is delegated to Textual's ``ContentSwitcher``: every tab view is mounted up front (so
first-visit latency is just the tab's own fetch, not widget construction), and switching tabs flips the
switcher's ``current`` to the right id. Combined with the VM's lazy filter propagation, switching to a
previously-visited tab that already matches the current filter is instantaneous; switching to a stale or
never-visited tab shows a "loading…" status while the tab fetches.

Tab-VM → tab-view mapping lives in ``_view_for_tab``: it's a flat dispatch on type, which is fine while
we have one concrete tab. When we add more, we'll either extend the dispatch table or have each tab VM
expose a ``make_view()`` factory — depends on whether the view ever needs construction params beyond the VM.
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
from .topic_summary import TopicSummaryView
from .topic_tree import BrowserTopicTreeView
from .view_model import BrowserViewModel

_logger = get_logger("browser.view")


def _view_for_tab(tab_vm: BrowserTabViewModel):
    """Return a freshly-constructed view widget for ``tab_vm``.

    Dispatches on the VM's concrete type. Raises if the VM type has no registered view — that's a programmer
    error (we added a new tab VM without adding a view), and silently rendering a blank tab would hide it.
    """
    if isinstance(tab_vm, KnowledgeEntryBrowserTabViewModel):
        return KnowledgeEntryBrowserTabView(tab_vm)
    raise TypeError(
        f"No view registered for tab VM {type(tab_vm).__name__}. "
        f"Add a branch to _view_for_tab in browser/view.py."
    )


class BrowserView(Horizontal):
    """Top-level browser widget. Takes an externally-constructed ``BrowserViewModel`` and drives it through
    its mount lifecycle.

    The VM is owned by the caller (typically the chat tab appends a fresh ``BrowserViewModel`` to its feed;
    this view is built against that instance). ``await self._vm.start()`` runs from ``on_mount`` to do the
    actual DB work after Textual has finished mounting child widgets — by then everyone is subscribed to
    their VM's ``dirty`` and will repaint on first data arrival.

    Construction example::

        vm = BrowserViewModel(session_factory)
        view = BrowserView(vm)
        await container.mount(view)
    """

    # ``height: 24`` is a deliberate fixed height for embedded use — when mounted inside a ``VerticalScroll``
    # feed, ``1fr`` resolves to 0 (the scroll container derives its content height from children, so a child
    # asking for "remaining" space has none to claim). 24 is the rough height of the legacy ``/explore``
    # tab; the user can tweak per-mount via CSS if a different surface needs another size.
    DEFAULT_CSS = """
    BrowserView {
        height: 40;
    }
    BrowserView #browser-tree-tab {
        width: 20%;
        border: solid #3a3a3a;
        padding: 0 0 0 1;
    }
    BrowserView #browser-tree-tab:focus-within {
        border: solid #6a6a6a;
    }
    BrowserView #browser-tree-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    BrowserView BrowserTopicTreeView {
        padding: 1 0 0 0;
        height: 1fr;
    }
    BrowserView #browser-tree-summary {
        height: auto;
        max-height: 50%;
        border-top: solid #3a3a3a;
        padding: 0 0 0 0;
    }
    BrowserView #browser-right-tab {
        width: 80%;
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
        # ``priority=True`` so these fire even when a deep descendant (e.g. a ``TextArea`` inside the details
        # panel) is focused — otherwise ``TextArea``'s own ``alt+left``/``alt+right`` word-navigation
        # bindings would swallow the event and our region cycle would only work when focus was on a
        # non-editor widget. ``alt+up``/``alt+down`` are bound at the same priority for the same
        # reason — TextArea has its own paragraph-jump bindings on these keys.
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
        # Left: topic tree. The tree view subscribes to the tree VM in its own on_mount, so all we do here
        # is hand it the VM.
        with Vertical(id="browser-tree-tab"):
            yield Static("Topics", id="browser-tree-title")
            yield BrowserTopicTreeView(self._vm.tree)
            # Topic summary panel for the cursor-highlighted topic. Sits below the tree, growing to fit
            # its (multi-line description) content; the tree above gets ``height: 1fr`` so it claims the
            # remaining vertical space and ``max-height: 50%`` on the summary keeps a long description
            # from squeezing the tree out of view.
            yield TopicSummaryView(self._vm.summary, id="browser-tree-summary")

        # Right: tab bar over a ContentSwitcher that holds every tab view. The tab lineup is fixed at ctor
        # time (see BrowserViewModel), so we can mount every tab view up front and just toggle which one is
        # current.
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
        # Now that all child widgets have mounted and subscribed to their respective VMs, kick off the data
        # load. start() triggers the tree's root load and the active tab's first fetch.
        await self._vm.start()
        # Give the tree focus by default so arrow keys work out of the gate.
        try:
            self.query_one(BrowserTopicTreeView).focus()
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    # Horizontal isn't focusable; route ``vm.request_focus()`` (fired by chat-tab feed nav) to the tree,
    # which is.
    def focus(self, scroll_visible: bool = True) -> "BrowserView":
        try:
            self.query_one(BrowserTopicTreeView).focus(scroll_visible=scroll_visible)
        except Exception:
            pass
        return self

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._update_tab_bar()
        self._update_visible_tab()

    def _update_tab_bar(self) -> None:
        """Render the tab bar as a Rich-styled text run: active tab is reverse-video, inactives are plain.
        Cheap and avoids needing a real tab widget while we have only one tab."""
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
    # ``BrowserView`` owns the two top-level regions — topic tree on the left, active tab on the right — and
    # delegates everything inside the tab to ``tab.nav_<dir>``. ``alt+right`` from the tree lands focus on
    # the tab's first region (``focus_first``); ``alt+left`` from the tab can return the sentinel
    # ``"topic_tree"`` to ask us to focus the tree. Up / down stay inside the tab.

    def action_focus_right(self) -> None:
        if self._focus_is_in_tree():
            tab = self._active_tab_view()
            if tab is not None and hasattr(tab, "focus_first"):
                tab.focus_first()
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_right"):
            tab.nav_right()

    def action_focus_left(self) -> None:
        if self._focus_is_in_tree():
            # Leftmost edge from the tree — hard no-op.
            return
        tab = self._active_tab_view()
        result = tab.nav_left() if tab is not None and hasattr(tab, "nav_left") else False
        if result == "topic_tree":
            try:
                self.query_one(BrowserTopicTreeView).focus()
            except Exception:
                pass

    def action_focus_up(self) -> None:
        if self._focus_is_in_tree():
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_up"):
            tab.nav_up()

    def action_focus_down(self) -> None:
        if self._focus_is_in_tree():
            return
        tab = self._active_tab_view()
        if tab is not None and hasattr(tab, "nav_down"):
            tab.nav_down()

    def _focus_is_in_tree(self) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        try:
            tree = self.query_one(BrowserTopicTreeView)
        except Exception:
            return False
        return focused is tree or tree in focused.ancestors_with_self

    def _active_tab_view(self):
        """Return the currently-visible tab view widget, or ``None`` if the active index is out of range.
        Each tab view is mounted with id ``browser-tab-{i}`` so we look it up by id."""
        target_id = self._tab_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
