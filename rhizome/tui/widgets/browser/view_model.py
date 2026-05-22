"""BrowserViewModel — orchestrator for the new browser widget.

Owns the topic tree VM and the ordered list of pane VMs (one per tab), plus
the active-tab index. The job of this class is *coordination*, not data:
each child VM is responsible for its own state, and the orchestrator just
arranges that:

  * when the user changes the tree selection, the active pane gets the
    expanded filter immediately;
  * inactive panes are caught up lazily on tab switch — their data and
    last-applied filter persist until then;
  * the active-tab index is published as a single ``dirty`` signal the
    parent view can repaint against.

Cross-VM coordination here is via direct method calls, per the
``ViewModelBase`` communication model: the BrowserVM subscribes to the
tree's ``SELECTION_CHANGED`` and calls ``set_filter`` directly on the
active pane. There is no "broadcast filter" callback group — that would
be one VM emitting on behalf of another.

Lazy propagation
----------------
Filter changes fan out only to the active pane. Inactive panes hold whatever
data they last rendered, plus the filter that produced it. On tab switch the
new active pane gets a ``set_filter`` with the current orchestrator filter;
because ``BrowserPaneViewModel.set_filter`` is idempotent on unchanged
filters, switching to a pane that already matches the current filter is an
instant no-op, while switching to a pane with a stale filter triggers a
refetch.

Async boundary: with cascade-on-toggle, the recursive-CTE expansion now
happens *inside* the tree VM's ``toggle_selection`` (the cascade is the
expansion). By the time ``SELECTION_CHANGED`` fires, the tree's
``_selected_ids`` is already the fully-expanded set, so the orchestrator's
handler is a synchronous read of ``expanded_filter_ids`` followed by a
direct ``set_filter`` on the active pane — no background task, no
cancellation dance.
"""

from __future__ import annotations

from typing import Any, Iterable

from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase
from .knowledge_entry_pane import KnowledgeEntryBrowserPaneViewModel
from .pane_base import BrowserPaneViewModel
from .topic_tree import BrowserTopicTreeViewModel

_logger = get_logger("browser")


def _default_panes(session_factory: Any) -> list[BrowserPaneViewModel]:
    """The production pane set, constructed at ctor time when the caller
    doesn't override. Kept as a free function so tests can patch or compare
    against it without instantiating a full BrowserViewModel."""
    return [KnowledgeEntryBrowserPaneViewModel(session_factory)]


class BrowserViewModel(ViewModelBase):
    """Top-level browser view-model.

    The pane lineup is fixed at construction. Pass ``panes=...`` to override
    the default (e.g. in tests); pass ``panes=None`` to get the production
    set from ``_default_panes``. Call ``await start()`` once after mounting
    to load the tree roots and seed the active pane with the empty filter.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        panes: Iterable[BrowserPaneViewModel] | None = None,
    ) -> None:
        super().__init__()
        self.is_navigable = True
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)
        self._panes: list[BrowserPaneViewModel] = []
        self._active_index: int = 0
        self._started: bool = False

        # ``_current_filter`` is the orchestrator's view of the live filter —
        # the active pane is brought in sync with this immediately; inactive
        # panes catch up via ``switch_pane``. ``frozenset`` ↔ ``None`` echoes
        # the pane-side filter semantics (None = no filter at all).
        self._current_filter: frozenset[int] | None = None

        # Wire the tree's selection signal into our propagation handler. This
        # is the one inter-VM subscription the orchestrator owns; everything
        # else is direct method calls.
        self._tree.subscribe(
            self._tree.selection_changed,
            self._on_selection_changed,
        )

        resolved_panes = _default_panes(session_factory) if panes is None else panes
        for pane in resolved_panes:
            self._add_pane(pane)

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    @property
    def panes(self) -> list[BrowserPaneViewModel]:
        return list(self._panes)

    @property
    def active_index(self) -> int:
        return self._active_index

    @property
    def active_pane(self) -> BrowserPaneViewModel | None:
        if 0 <= self._active_index < len(self._panes):
            return self._panes[self._active_index]
        return None

    @property
    def current_filter(self) -> frozenset[int] | None:
        return self._current_filter

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Seed the active pane with the current (empty) filter.

        The tree view loads its own roots on mount — the VM doesn't cache
        tree shape, so there's nothing for the orchestrator to await.
        Idempotent: a second call is a no-op.
        """
        if self._started:
            return
        self._started = True
        # Seed only the active pane. Inactive panes stay empty until the
        # user switches to them (see ``switch_pane``).
        active = self.active_pane
        if active is not None:
            active.set_filter(self._current_filter)

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    def _add_pane(self, pane: BrowserPaneViewModel) -> None:
        """Append a pane to the tab list. Private — the pane lineup is fixed
        at construction. Called from ``__init__`` only."""
        self._panes.append(pane)
        self.emit(self.dirty)

    def switch_pane(self, index: int) -> None:
        """Activate the pane at ``index``. No-op if already active or out of
        range — out-of-range is a programmer error, but we'd rather log and
        ignore than crash a UI handler.

        Catches the newly-active pane up to ``_current_filter`` via
        ``set_filter``; the call is a no-op if the pane already holds data
        for that filter (instant switch), or triggers a refetch if not.
        """
        if index == self._active_index:
            return
        if not 0 <= index < len(self._panes):
            _logger.warning(
                "switch_pane: index %d out of range (have %d panes)",
                index, len(self._panes),
            )
            return
        self._active_index = index
        # Only sync the new active pane with the current filter after we've
        # started — before start() the tree hasn't loaded, ``_current_filter``
        # is still ``None`` by default, and nobody should be switching panes
        # anyway.
        if self._started:
            self._panes[index].set_filter(self._current_filter)
        self.emit(self.dirty)

    def next_pane(self) -> None:
        """Activate the next pane, wrapping past the end back to index 0."""
        if not self._panes:
            return
        self.switch_pane((self._active_index + 1) % len(self._panes))

    def prev_pane(self) -> None:
        """Activate the previous pane, wrapping past 0 back to the last index."""
        if not self._panes:
            return
        self.switch_pane((self._active_index - 1) % len(self._panes))

    # ------------------------------------------------------------------
    # Selection propagation
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """Sync callback fired by the tree on every cascade-toggle.

        Cascade-on-toggle pre-expanded the selection inside the tree VM, so
        there's no async work left here: read the now-expanded filter set
        and hand it straight to the active pane. Inactive panes catch up
        lazily on ``switch_pane`` via the idempotent ``set_filter``.
        """
        self._current_filter = self._tree.expanded_filter_ids()
        active = self.active_pane
        if active is not None:
            active.set_filter(self._current_filter)
