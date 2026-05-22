"""BrowserView — top-level view for the new browser widget.

Horizontal layout: topic tree on the left, tab bar + active pane on the
right. Bootstraps the ``BrowserViewModel`` on mount and lets the VM drive
which pane is visible.

Pane visibility is delegated to Textual's ``ContentSwitcher``: every pane
view is mounted up front (so first-visit latency is just the pane's own
fetch, not widget construction), and switching tabs flips the switcher's
``current`` to the right id. Combined with the VM's lazy filter
propagation, switching to a previously-visited pane that already matches
the current filter is instantaneous; switching to a stale or never-visited
pane shows a "loading…" status while the pane fetches.

Pane-VM → pane-view mapping lives in ``_view_for_pane``: it's a flat
dispatch on type, which is fine while we have one concrete pane. When we
add more, we'll either extend the dispatch table or have each pane VM
expose a ``make_view()`` factory — depends on whether the view ever needs
construction params beyond the VM.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Static

from rhizome.logs import get_logger

from .knowledge_entry_pane import (
    KnowledgeEntryBrowserPaneView,
    KnowledgeEntryBrowserPaneViewModel,
)
from .pane_base import BrowserPaneViewModel
from .topic_tree import BrowserTopicTreeView
from .view_model import BrowserViewModel

_logger = get_logger("browser.view")


def _view_for_pane(pane_vm: BrowserPaneViewModel):
    """Return a freshly-constructed view widget for ``pane_vm``.

    Dispatches on the VM's concrete type. Raises if the VM type has no
    registered view — that's a programmer error (we added a new pane VM
    without adding a view), and silently rendering a blank pane would
    hide it.
    """
    if isinstance(pane_vm, KnowledgeEntryBrowserPaneViewModel):
        return KnowledgeEntryBrowserPaneView(pane_vm)
    raise TypeError(
        f"No view registered for pane VM {type(pane_vm).__name__}. "
        f"Add a branch to _view_for_pane in browser/view.py."
    )


class BrowserView(Horizontal):
    """Top-level browser widget. Takes an externally-constructed
    ``BrowserViewModel`` and drives it through its mount lifecycle.

    The VM is owned by the caller (typically the chat pane appends a fresh
    ``BrowserViewModel`` to its feed; this view is built against that
    instance). ``await self._vm.start()`` runs from ``on_mount`` to do the
    actual DB work after Textual has finished mounting child widgets — by
    then everyone is subscribed to their VM's ``dirty`` and will repaint
    on first data arrival.

    Construction example::

        vm = BrowserViewModel(session_factory)
        view = BrowserView(vm)
        await container.mount(view)
    """

    # ``height: 24`` is a deliberate fixed height for embedded use — when
    # mounted inside a ``VerticalScroll`` feed, ``1fr`` resolves to 0 (the
    # scroll container derives its content height from children, so a child
    # asking for "remaining" space has none to claim). 24 is the rough
    # height of the legacy ``/explore`` pane; the user can tweak per-mount
    # via CSS if a different surface needs another size.
    DEFAULT_CSS = """
    BrowserView {
        height: 30;
    }
    BrowserView #browser-tree-pane {
        width: 20%;
        border: solid #3a3a3a;
        padding: 0 0 0 1;
    }
    BrowserView #browser-tree-pane:focus-within {
        border: solid #6a6a6a;
    }
    BrowserView #browser-tree-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    BrowserView BrowserTopicTreeView {
        padding: 1 0 0 0;
    }
    BrowserView #browser-right-pane {
        width: 80%;
        height: 1fr;
        border: solid #3a3a3a;
    }
    BrowserView #browser-right-pane:focus-within {
        border: solid #6a6a6a;
    }
    BrowserView #browser-tab-bar {
        height: 1;
        padding: 0 1;
    }
    BrowserView #browser-pane-area {
        height: 1fr;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+right", "next_pane", show=False),
        Binding("ctrl+left", "prev_pane", show=False),
        # ``priority=True`` so these fire even when a deep descendant
        # (e.g. a ``TextArea`` inside the details panel) is focused —
        # otherwise ``TextArea``'s own ``alt+left``/``alt+right`` word-
        # navigation bindings would swallow the event and our region
        # cycle would only work when focus was on a non-editor widget.
        Binding("alt+right", "focus_right", priority=True, show=False),
        Binding("alt+left", "focus_left", priority=True, show=False),
    ]

    def __init__(self, view_model: BrowserViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    @property
    def view_model(self) -> BrowserViewModel:
        return self._vm

    def compose(self):
        # Left: topic tree. The tree view subscribes to the tree VM in its
        # own on_mount, so all we do here is hand it the VM.
        with Vertical(id="browser-tree-pane"):
            yield Static("Topics", id="browser-tree-title")
            yield BrowserTopicTreeView(self._vm.tree)

        # Right: tab bar over a ContentSwitcher that holds every pane view.
        # The pane lineup is fixed at ctor time (see BrowserViewModel), so
        # we can mount every pane view up front and just toggle which one
        # is current.
        with Vertical(id="browser-right-pane"):
            yield Static("", id="browser-tab-bar")
            initial_id = self._pane_widget_id(self._vm.active_index)
            with ContentSwitcher(initial=initial_id, id="browser-pane-area"):
                for index, pane_vm in enumerate(self._vm.panes):
                    pane_view = _view_for_pane(pane_vm)
                    pane_view.id = self._pane_widget_id(index)
                    yield pane_view

    async def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.subscribe(self._vm.focus, self.focus)
        # Initial paint of the tab bar — the VM hasn't emitted dirty yet, so
        # do it ourselves.
        self._refresh()
        # Now that all child widgets have mounted and subscribed to their
        # respective VMs, kick off the data load. start() triggers the tree's
        # root load and the active pane's first fetch.
        await self._vm.start()
        # Give the tree focus by default so arrow keys work out of the gate.
        try:
            self.query_one(BrowserTopicTreeView).focus()
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    # Horizontal isn't focusable; route ``vm.request_focus()`` (fired by chat-pane
    # feed nav) to the tree, which is.
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
        self._update_visible_pane()

    def _update_tab_bar(self) -> None:
        """Render the tab bar as a Rich-styled text run: active tab is
        reverse-video, inactives are plain. Cheap and avoids needing a real
        tab widget while we have only one pane."""
        tab_bar = self.query_one("#browser-tab-bar", Static)
        text = Text()
        for i, pane in enumerate(self._vm.panes):
            if i > 0:
                text.append("   ")
            if i == self._vm.active_index:
                text.append(f" {pane.title} ", style="bold")
            else:
                text.append(f" {pane.title} ", style="dim")
        tab_bar.update(text)

    def _update_visible_pane(self) -> None:
        switcher = self.query_one("#browser-pane-area", ContentSwitcher)
        target = self._pane_widget_id(self._vm.active_index)
        if switcher.current != target:
            switcher.current = target

    @staticmethod
    def _pane_widget_id(index: int) -> str:
        return f"browser-pane-{index}"

    # ------------------------------------------------------------------
    # Key actions
    # ------------------------------------------------------------------

    def action_next_pane(self) -> None:
        self._vm.next_pane()

    def action_prev_pane(self) -> None:
        self._vm.prev_pane()

    # ------------------------------------------------------------------
    # Cross-region focus navigation (alt+left/right)
    # ------------------------------------------------------------------
    #
    # Topology:
    #
    #   [Topic tree]  <->  [Active pane's internal cycle]
    #
    # ``alt+right`` advances; ``alt+left`` retreats. The browser view
    # only knows about the two top-level regions (tree, pane) — each
    # pane view is responsible for its own internal sub-region cycle
    # and signals "I'm at my edge" back to us by returning False from
    # ``focus_next_region`` / ``focus_prev_region``.

    def action_focus_right(self) -> None:
        if self._focus_is_in_tree():
            pane = self._active_pane_view()
            if pane is not None and hasattr(pane, "focus_first"):
                pane.focus_first()
            return
        pane = self._active_pane_view()
        if pane is not None and hasattr(pane, "focus_next_region"):
            pane.focus_next_region()
        # If the pane returns False we're at the rightmost edge of the
        # browser — there's nowhere further to go, so do nothing.

    def action_focus_left(self) -> None:
        if self._focus_is_in_tree():
            # Leftmost edge from the tree — hard no-op.
            return
        pane = self._active_pane_view()
        moved = (
            pane.focus_prev_region()
            if pane is not None and hasattr(pane, "focus_prev_region")
            else False
        )
        if not moved:
            # Pane has nothing more to its left — hand focus back to the tree.
            try:
                self.query_one(BrowserTopicTreeView).focus()
            except Exception:
                pass

    def _focus_is_in_tree(self) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        try:
            tree = self.query_one(BrowserTopicTreeView)
        except Exception:
            return False
        return focused is tree or tree in focused.ancestors_with_self

    def _active_pane_view(self):
        """Return the currently-visible pane view widget, or ``None`` if
        the active index is out of range. Each pane view is mounted with
        id ``browser-pane-{i}`` so we look it up by id."""
        target_id = self._pane_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
