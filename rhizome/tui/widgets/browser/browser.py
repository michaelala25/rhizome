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

from rich.text import Text

from textual.actions import SkipAction
from textual.containers import Vertical
from textual.events import DescendantBlur, DescendantFocus
from textual.message import Message
from textual.widgets import ContentSwitcher, Static

from rhizome.logs import get_logger

from rhizome.app.browser.tabs.entries.tab import EntryTabModel
from rhizome.tui.widgets.browser.tabs.entries.tab import EntryTab
from rhizome.app.browser.tab_base import BrowserTabModel
from rhizome.tui.widgets.browser.topics.panel import TopicTreePanel
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind
from rhizome.app.browser.browser import BrowserModel

_logger = get_logger("browser.view")


def _view_for_tab(tab_vm: BrowserTabModel):
    """Construct the view widget for ``tab_vm`` by dispatching on its concrete type. Raises rather
    than rendering a blank tab if a VM type has no registered view — that's a programmer error."""
    if isinstance(tab_vm, EntryTabModel):
        return EntryTab(tab_vm)
    raise TypeError(
        f"No view registered for tab VM {type(tab_vm).__name__}. "
        f"Add a branch to _view_for_tab in browser/view.py."
    )


class Browser(NavigableFeedItemViewBase[BrowserModel], FocusOrchestrationMixin):
    """Takes a caller-constructed ``BrowserModel`` and drives it through its mount lifecycle.

    ``await self._vm.start()`` runs from ``on_mount`` after Textual finishes mounting children — by
    then every child widget is subscribed to its VM's ``dirty`` and will repaint on first arrival.
    """

    class Dismissed(Message):
        """Public-surface dismiss request — the feed host catches this and drops the feed entry.
        Posted by the ``ctrl+c`` binding via ``action_dismiss``."""

        def __init__(self, browser: "Browser") -> None:
            super().__init__()
            self.browser = browser

        @property
        def control(self) -> "Browser":
            return self.browser

    # Height is pinned to 40 because inside the chat-tab ``VerticalScroll`` feed, ``1fr`` resolves
    # to 0 (the scroll container derives content height from children, so a child asking for
    # "remaining" gets none) — 40 fits the body comfortably; callers can override per-mount via CSS.
    DEFAULT_CSS = """
    Browser {
        height: 40;
        width: 1fr;
        layout: horizontal;
        overflow: hidden hidden;
    }
    Browser #browser-right-tab {
        width: 1fr;
        height: 1fr;
        border: solid #3a3a3a;
    }
    /* ``-focus-within`` is set manually from ``on_descendant_focus``/``on_descendant_blur`` rather
       than via the ``:focus-within`` pseudo-selector. Pseudo-classes invalidate every ancestor on
       any focus shift in the subtree; a class flip is local to the node that toggled it. */
    Browser #browser-right-tab.-focus-within {
        border: solid #6a6a6a;
    }
    Browser #browser-tab-bar {
        height: 1;
        padding: 0 1;
    }
    Browser #browser-tab-area {
        height: 1fr;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Keybind.BrowserNextTab.as_binding("next_tab", show=False),
        Keybind.BrowserPrevTab.as_binding("prev_tab", show=False),
        Keybind.BrowserDismiss.as_binding("dismiss", show=False),
        # Cross-region fall-through only — fires when the focused region's own alt+arrow action
        # raised SkipAction (i.e., the step had no in-graph target). Up/down have no cross-region
        # meaning, so they're left for the regions alone.
        Keybind.InnerFocusRight.as_binding("focus_neighbour('right')", show=False),
        Keybind.InnerFocusLeft .as_binding("focus_neighbour('left')",  show=False),
    ]

    def compose(self):
        yield TopicTreePanel(self._vm.panel, id="topic-tree-panel")
        with Vertical(id="browser-right-tab"):
            yield Static("", id="browser-tab-bar")
            initial_id = self._tab_widget_id(self._vm.active_index)
            with ContentSwitcher(initial=initial_id, id="browser-tab-area"):
                for index, tab_vm in enumerate(self._vm.tabs):
                    tab_view = _view_for_tab(tab_vm)
                    tab_view.id = self._tab_widget_id(index)
                    yield tab_view

    async def on_mount(self) -> None:
        # The VM hasn't emitted dirty yet, so paint the tab bar once ourselves before fetching.
        self._refresh()
        await self._vm.start()
        self.focus()

    # ------------------------------------------------------------------
    # Focus-within tracking — inline ``styles.border`` on the right tab (no CSS class involved)
    # ------------------------------------------------------------------
    #
    # Textual exposes ``on_descendant_focus`` and ``on_descendant_blur``, but neither is a clean signal
    # for "focus entered subtree" or "focus left subtree" — both fire on every focus shift among
    # descendants, including sibling-to-sibling moves where focus stays inside. The asymmetry that
    # matters is in the post-conditions: after a descendant-focus event, focus is *definitely* inside
    # the subtree (the event is its own proof). A descendant-blur is ambiguous, but it doesn't matter
    # here — both handlers funnel into the same ``screen.focused``-derived check.
    #
    # The border color is set via the right-tab's ``styles.border`` rather than by toggling a class.
    # Inline styles are node-scoped, so Textual doesn't re-evaluate descendant selectors when they
    # change — avoiding the defensive subtree reapply that a class change triggers on a node with
    # many descendants (the right tab holds EntryTable + EntryDetails + EntryPreview, all heavy).
    #
    # The right-tab container is a plain ``Vertical`` with an id (not a custom Widget subclass), so
    # the handlers live here on ``Browser`` and assign to the queried element's ``styles``.

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self._sync_right_tab_focus_within()

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        self._sync_right_tab_focus_within()

    def _sync_right_tab_focus_within(self) -> None:
        try:
            right_tab = self.query_one("#browser-right-tab")
        except Exception:
            return
        focused = self.screen.focused if self.screen else None
        inside = focused is not None and (
            focused is right_tab or right_tab in focused.ancestors_with_self
        )
        right_tab.styles.border = ("solid", "#6a6a6a" if inside else "#3a3a3a")

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

    def action_dismiss(self) -> None:
        # Feed host catches ``Browser.Dismissed`` and drops the feed entry — the only removal path.
        self.post_message(self.Dismissed(self))

    # ------------------------------------------------------------------
    # Cross-region focus graph — two nodes (panel, active tab), two edges (panel ↔ tab)
    # ------------------------------------------------------------------
    #
    # The action fires when a focused region's own ``alt+arrow`` returned None and raised
    # ``SkipAction`` because the step had no in-graph next step. Up/down have no cross-region
    # meaning, so they aren't bound here at all.

    def action_focus_neighbour(self, direction: str) -> None:
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    # Graph is dynamic in ``vm.tabs``: ``topic-tree-panel`` right → every tab id as a fallback
    # candidate, and ``_is_node_available`` keeps only the one ContentSwitcher currently shows.
    # Each tab's left always points back to the panel. When tab count is 1 the fallback list is
    # degenerate, but the shape is correct as soon as more tab VMs are added.
    def _get_focus_graph(self) -> FocusGraph:
        tab_ids = [self._tab_widget_id(i) for i in range(len(self._vm.tabs))]
        edges: dict[str, dict[str, str | list[str]]] = {
            "topic-tree-panel": {"right": tab_ids},
        }
        for tid in tab_ids:
            edges[tid] = {"left": "topic-tree-panel"}
        return FocusGraph(source="topic-tree-panel", edges=edges)

    def _is_node_available(self, node_id: str) -> bool:
        # Panel always available. Tab nodes are gated by whichever the ContentSwitcher is showing.
        if node_id == "topic-tree-panel":
            return True
        try:
            switcher = self.query_one("#browser-tab-area", ContentSwitcher)
        except Exception:
            return False
        return node_id == switcher.current
