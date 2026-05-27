"""BrowserViewModel — orchestrator for the new browser widget.

Owns the topic-tree panel and the ordered list of tab VMs (one per tab), plus the active-tab index.
The orchestrator's job is *coordination*, not data: the panel owns the tree / actions / summary and
their internal wiring, each tab owns its own state, and this class just arranges that:

  * when the panel reports a filter change, the active tab gets the new filter immediately;
  * inactive tabs are caught up lazily on tab switch — their data and last-applied filter persist
    until then;
  * the active-tab index is published as a single ``dirty`` signal the parent view can repaint
    against.

Cross-VM coordination here is via direct method calls, per the ``ViewModelBase`` communication
model: the orchestrator subscribes to ``panel.filter_changed`` and calls ``set_topic_filter``
directly on the active tab. There is no "broadcast filter" callback group — that would be one VM
emitting on behalf of another.

Lazy propagation
----------------
Filter changes fan out only to the active tab. Inactive tabs hold whatever data they last rendered,
plus the filter that produced it. On tab switch the new active tab gets a ``set_topic_filter`` with
the panel's current filter; because ``BrowserTabViewModel.set_topic_filter`` is idempotent on
unchanged filters, switching to a tab that already matches the current filter is an instant no-op,
while switching to a tab with a stale filter triggers a refetch.
"""

from __future__ import annotations

from typing import Any, Iterable

from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase
from .knowledge_entry_tab import KnowledgeEntryBrowserTabViewModel
from .tab_base import BrowserTabViewModel
from .topic_tree_panel import TopicTreePanelViewModel

_logger = get_logger("browser")


def _default_tabs(session_factory: Any) -> list[BrowserTabViewModel]:
    """The production tab set, constructed at ctor time when the caller doesn't override. Kept as a
    free function so tests can patch or compare against it without instantiating a full
    BrowserViewModel."""
    return [KnowledgeEntryBrowserTabViewModel(session_factory)]


class BrowserViewModel(ViewModelBase):
    """Top-level browser view-model.

    The tab lineup is fixed at construction. Pass ``tabs=...`` to override the default (e.g. in
    tests); pass ``tabs=None`` to get the production set from ``_default_tabs``. Call ``await
    start()`` once after mounting to seed the active tab with the panel's current (empty) filter.
    """

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

        # Wire the panel's filter signal into our propagation handler. This is the one inter-VM
        # subscription the orchestrator owns; everything else is direct method calls. The panel
        # owns the tree-cursor → summary subscription internally — the orchestrator never sees it.
        self._panel.subscribe(
            self._panel.filter_changed,
            self._on_filter_changed,
        )

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
        """Convenience pass-through to ``panel.current_filter``. Useful for tests and any caller
        that wants the filter without reaching through ``vm.panel``."""
        return self._panel.current_filter

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Seed the active tab with the current (empty) filter.

        The panel's child views load their own state on mount — the orchestrator doesn't await any
        DB work here. Idempotent: a second call is a no-op.
        """
        if self._started:
            return
        self._started = True
        # Seed only the active tab. Inactive tabs stay empty until the user switches to them (see
        # ``switch_tab``).
        active = self.active_tab
        if active is not None:
            active.set_topic_filter(self._panel.current_filter)

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def _add_tab(self, tab: BrowserTabViewModel) -> None:
        """Append a tab to the tab list. Private — the tab lineup is fixed at construction. Called
        from ``__init__`` only."""
        self._tabs.append(tab)
        self.emit(self.dirty)

    def switch_tab(self, index: int) -> None:
        """Activate the tab at ``index``. No-op if already active or out of range — out-of-range is
        a programmer error, but we'd rather log and ignore than crash a UI handler.

        Catches the newly-active tab up to the panel's current filter via ``set_topic_filter``; the
        call is a no-op if the tab already holds data for that filter (instant switch), or triggers
        a refetch if not.
        """
        if index == self._active_index:
            return
        if not 0 <= index < len(self._tabs):
            _logger.warning(
                "switch_tab: index %d out of range (have %d tabs)",
                index, len(self._tabs),
            )
            return
        self._active_index = index
        # Only sync the new active tab with the current filter after we've started — before
        # ``start()`` the panel hasn't published anything meaningful, and nobody should be switching
        # tabs anyway.
        if self._started:
            self._tabs[index].set_topic_filter(self._panel.current_filter)
        self.emit(self.dirty)

    def next_tab(self) -> None:
        """Activate the next tab, wrapping past the end back to index 0."""
        if not self._tabs:
            return
        self.switch_tab((self._active_index + 1) % len(self._tabs))

    def prev_tab(self) -> None:
        """Activate the previous tab, wrapping past 0 back to the last index."""
        if not self._tabs:
            return
        self.switch_tab((self._active_index - 1) % len(self._tabs))

    # ------------------------------------------------------------------
    # Filter propagation
    # ------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        """Sync callback fired by the panel on every selection-toggle (re-emitted from the tree's
        ``SELECTION_CHANGED``). Pushes the panel's now-current filter into the active tab. Inactive
        tabs catch up lazily on ``switch_tab`` via the idempotent ``set_topic_filter``.
        """
        active = self.active_tab
        if active is not None:
            active.set_topic_filter(self._panel.current_filter)
