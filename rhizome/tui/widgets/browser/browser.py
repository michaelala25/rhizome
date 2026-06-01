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
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import DescendantBlur, DescendantFocus
from textual.widgets import ContentSwitcher, Static

from rhizome.logs import get_logger

from rhizome.app.browser.tabs.entries.tab import EntryTabVM
from rhizome.tui.widgets.browser.tabs.entries.tab import EntryTab
from rhizome.app.browser.tab_base import BrowserTabVM
from rhizome.tui.widgets.browser.topics.panel import TopicTreePanel
from rhizome.tui.widgets.navigable_feed_item_view_base import NavigableFeedItemViewBase
from rhizome.app.browser.browser import BrowserVM

_logger = get_logger("browser.view")


def _view_for_tab(tab_vm: BrowserTabVM):
    """Construct the view widget for ``tab_vm`` by dispatching on its concrete type. Raises rather
    than rendering a blank tab if a VM type has no registered view — that's a programmer error."""
    if isinstance(tab_vm, EntryTabVM):
        return EntryTab(tab_vm)
    raise TypeError(
        f"No view registered for tab VM {type(tab_vm).__name__}. "
        f"Add a branch to _view_for_tab in browser/view.py."
    )


class Browser(NavigableFeedItemViewBase[BrowserVM]):
    """Takes a caller-constructed ``BrowserVM`` and drives it through its mount lifecycle.

    ``await self._vm.start()`` runs from ``on_mount`` after Textual finishes mounting children — by
    then every child widget is subscribed to its VM's ``dirty`` and will repaint on first arrival.
    """

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
        Binding("ctrl+right", "next_tab", show=False),
        Binding("ctrl+left", "prev_tab", show=False),
        # Cross-region fall-through only — fires when the focused region's own alt+arrow action
        # raised SkipAction (i.e., the step had no in-graph target). Up/down have no cross-region
        # meaning, so they're left for the regions alone.
        Binding("alt+right", "nav_right", show=False),
        Binding("alt+left", "nav_left", show=False),
    ]

    def compose(self):
        yield TopicTreePanel(self._vm.panel)
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

    def on_focus(self, event) -> None:
        # Bounce focus inward off the bare container onto the topic panel. No ``super()`` call —
        # Textual auto-dispatches ``on_focus`` at every MRO level, so ``ViewBase.on_focus`` (VM
        # notify) and ``NavigableFeedItemViewBase.on_focus`` (inline border sync) already fire on
        # their own. An explicit ``super().on_focus(event)`` here double-fires them.
        panel = self._panel_view()
        if panel is not None:
            panel.focus()

    # Browser is a composite — focus belongs on its inner regions, not the outer container. Route
    # external focus requests (including the ``vm.focus → self.focus`` subscription wired by
    # ``ViewBase``) through the panel, which in turn lands focus on the topic tree via its own
    # ``focus()`` override.
    def focus(self, scroll_visible: bool = True) -> "Browser":
        panel = self._panel_view()
        if panel is not None:
            panel.focus()
        return self

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

    def _panel_view(self) -> TopicTreePanel | None:
        try:
            return self.query_one(TopicTreePanel)
        except Exception:
            return None

    def _active_tab_view(self):
        target_id = self._tab_widget_id(self._vm.active_index)
        try:
            return self.query_one(f"#{target_id}")
        except Exception:
            return None
