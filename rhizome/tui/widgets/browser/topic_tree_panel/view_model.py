"""TopicTreePanelViewModel — bundles the topic tree, actions menu, and summary into a single panel
VM for the browser's left rail.

The panel exists so the top-level ``BrowserViewModel`` doesn't have to know about three separate
children and the wires between them. Internally:

  * ``tree.cursor_changed`` → ``summary.set_topic_id`` — cursor moves drive the summary fetch
  * ``tree.selection_changed`` → re-emit as ``filter_changed`` — selection changes drive the active
    tab's filter; re-emitting through our own callback group means the orchestrator subscribes to
    the panel, not into ``self.tree``.

No async ``start()`` of its own — the tree view loads roots on mount, and the summary fetches on
first cursor event. Construction wires the subscriptions; the orchestrator only ever talks to the
panel via ``filter_changed`` and ``current_filter``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rhizome.logs import get_logger

from ..topic_summary import TopicSummaryViewModel
from ..topic_tree import BrowserTopicTreeViewModel
from ...view_model_base import ViewModelBase
from .topic_tree_actions import TopicTreeActionsViewModel

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanelViewModel(ViewModelBase):
    """Owns the three left-rail child VMs and exposes the panel's contract to the browser
    orchestrator.

    Read-only accessors for each child VM are kept (``tree``, ``tree_actions``, ``summary``) so the
    panel view can hand each one to its corresponding sub-view at compose time. Beyond that, the
    panel surface is the ``filter_changed`` callback group and the ``current_filter`` sync property.
    """

    class Callbacks(Enum):
        # Fires when the tree selection changes — i.e. the topic-id filter the tabs should apply
        # has been updated. The orchestrator subscribes to this and pushes ``current_filter`` into
        # the active tab.
        FILTER_CHANGED = "filter_changed"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)
        self._tree_actions = TopicTreeActionsViewModel(session_factory, self._tree)
        self._summary = TopicSummaryViewModel(session_factory)

        self._filter_changed = self._make_group(self.Callbacks.FILTER_CHANGED)

        # tree → summary: cursor drives the summary panel.
        self._tree.subscribe(
            self._tree.cursor_changed,
            self._on_cursor_changed,
        )
        # tree → orchestrator (via this panel): selection drives the active tab's filter. Re-emit
        # through our own callback group so the orchestrator doesn't need to reach into ``tree``.
        self._tree.subscribe(
            self._tree.selection_changed,
            self._on_selection_changed,
        )

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    @property
    def tree_actions(self) -> TopicTreeActionsViewModel:
        return self._tree_actions

    @property
    def summary(self) -> TopicSummaryViewModel:
        return self._summary

    @property
    def filter_changed(self):
        return self._filter_changed

    @property
    def current_filter(self) -> frozenset[int] | None:
        """Sync read of the topic-id filter expressed by the current tree selection.

        ``None`` means "no filter — show everything" (the empty-selection state); otherwise returns
        the fully-expanded subtree id set. Matches ``BrowserTopicTreeViewModel.expanded_filter_ids``
        semantics — see the tree VM docstring for details.
        """
        return self._tree.expanded_filter_ids()

    # ------------------------------------------------------------------
    # Internal wiring
    # ------------------------------------------------------------------

    def _on_cursor_changed(self) -> None:
        self._summary.set_topic_id(self._tree.cursor_topic_id)

    def _on_selection_changed(self) -> None:
        # No payload — listeners re-query ``current_filter``. Single emit per cascade-toggle (the
        # tree's ``SELECTION_CHANGED`` already coalesces the cascade into one fire).
        self.emit(self._filter_changed)
