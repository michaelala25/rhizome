"""Orchestrator for the browser widget. Owns the topic-tree panel VM and an ordered list of tab
VMs plus the active-tab index. Pure coordination — the panel and each tab own their own state.

Filter propagation is **lazy**: on every tree ``selection_changed`` only the active tab is updated.
Inactive tabs hold their data and last-applied filter until the user switches to them, at which
point ``set_topic_filter`` reconciles them. Because that call is idempotent on equal filters,
switching to an already-current tab is an instant no-op.
"""

from __future__ import annotations

from typing import Any, Iterable

from rhizome.logs import get_logger

from rhizome.app.vm import ViewModelBase
from .knowledge_entry_tab import KnowledgeEntryBrowserTabViewModel
from .tab_base import BrowserTabViewModel
from .topic_tree_panel import TopicTreePanelViewModel

_logger = get_logger("browser")


def _default_tabs(session_factory: Any) -> list[BrowserTabViewModel]:
    # Free function so tests can patch or compare against the production set without instantiating
    # a full BrowserViewModel.
    return [KnowledgeEntryBrowserTabViewModel(session_factory)]


class BrowserViewModel(ViewModelBase):
    """Tab lineup is fixed at construction (``tabs=None`` → the production set). Call ``await
    start()`` once after mounting to seed the active tab with the panel's current filter."""

    def __init__(
        self,
        session_factory: Any,
        *,
        tabs: Iterable[BrowserTabViewModel] | None = None,
    ) -> None:
        super().__init__()
        self.is_navigable = True
        self._session_factory = session_factory
        self._panel = TopicTreePanelViewModel(session_factory)
        self._tabs: list[BrowserTabViewModel] = []
        self._active_index: int = 0
        self._started: bool = False

        # Inter-VM subscriptions the orchestrator owns; everything else is direct method calls.
        # Subscribed directly on the tree — the panel doesn't re-broadcast under an alias. The
        # details ``saved`` hook fires after a successful rename/edit so we can refresh any tab
        # rows that may now carry stale topic names.
        self._panel.tree.subscribe(self._panel.tree.selection_changed, self._on_filter_changed)
        self._panel.details.subscribe(self._panel.details.saved, self._on_topic_saved)
        self._panel.tree.subscribe(self._panel.tree.topic_deleted, self._on_topic_deleted)

        resolved_tabs = _default_tabs(session_factory) if tabs is None else tabs
        for tab in resolved_tabs:
            self._add_tab(tab)

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def panel(self) -> TopicTreePanelViewModel:
        return self._panel

    @property
    def tabs(self) -> list[BrowserTabViewModel]:
        return list(self._tabs)

    @property
    def active_index(self) -> int:
        return self._active_index

    @property
    def active_tab(self) -> BrowserTabViewModel | None:
        if 0 <= self._active_index < len(self._tabs):
            return self._tabs[self._active_index]
        return None

    @property
    def current_filter(self) -> frozenset[int] | None:
        return self._panel.current_filter

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Seed only the active tab; idempotent. Inactive tabs stay empty until the user switches."""
        if self._started:
            return
        self._started = True
        active = self.active_tab
        if active is not None:
            active.set_topic_filter(self._panel.current_filter)

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def _add_tab(self, tab: BrowserTabViewModel) -> None:
        self._tabs.append(tab)
        self.emit(self.dirty)

    def switch_tab(self, index: int) -> None:
        if index == self._active_index:
            return
        if not 0 <= index < len(self._tabs):
            _logger.warning(
                "switch_tab: index %d out of range (have %d tabs)", index, len(self._tabs),
            )
            return
        self._active_index = index
        # Skip the filter push before ``start()`` — nothing meaningful has been published yet.
        if self._started:
            self._tabs[index].set_topic_filter(self._panel.current_filter)
        self.emit(self.dirty)

    def next_tab(self) -> None:
        if not self._tabs:
            return
        self.switch_tab((self._active_index + 1) % len(self._tabs))

    def prev_tab(self) -> None:
        if not self._tabs:
            return
        self.switch_tab((self._active_index - 1) % len(self._tabs))

    # ------------------------------------------------------------------
    # Filter propagation
    # ------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        # Fired by the tree's SELECTION_CHANGED. Only the active tab updates; inactive tabs catch
        # up lazily on switch.
        active = self.active_tab
        if active is not None:
            active.set_topic_filter(self._panel.current_filter)

    def _on_topic_saved(self) -> None:
        # Fired by panel.details.SAVED after a rename/edit. Re-run the active tab's query so any
        # rows joined against the renamed topic pick up the new name. Inactive tabs are left
        # untouched — they'll refetch the next time their filter changes; if a user switches to
        # one before that, they may briefly see stale topic names.
        active = self.active_tab
        if active is not None:
            active.refetch()

    def _on_topic_deleted(self) -> None:
        # Fired after a subtree delete commits. Active tab refetches so rows whose topic just
        # vanished (or whose entries cascaded away) disappear from view. Same inactive-tab
        # caveat as rename — they'll catch up on their next filter change.
        active = self.active_tab
        if active is not None:
            active.refetch()
