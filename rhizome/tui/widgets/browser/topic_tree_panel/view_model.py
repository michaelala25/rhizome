"""TopicTreePanelViewModel — bundles tree + actions + summary as the browser's left-rail VM.

Internal wiring: ``tree.cursor_changed`` → ``summary.set_topic_id``; ``tree.selection_changed``
re-emitted as the panel's own ``filter_changed`` so the orchestrator subscribes at the panel
boundary instead of reaching into ``panel.tree``.
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
    """Owns the three rail child VMs and exposes the panel-level contract to the orchestrator.

    The child VMs are exposed read-only so the panel view can hand each one to its sub-view at
    compose time; the orchestrator only touches ``filter_changed`` and ``current_filter``.
    """

    class Callbacks(Enum):
        FILTER_CHANGED = "filter_changed"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)
        self._tree_actions = TopicTreeActionsViewModel(session_factory, self._tree)
        self._summary = TopicSummaryViewModel(session_factory)

        self._filter_changed = self._make_group(self.Callbacks.FILTER_CHANGED)

        self._tree.subscribe(self._tree.cursor_changed, self._on_cursor_changed)
        self._tree.subscribe(self._tree.selection_changed, self._on_selection_changed)

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
        """Topic-id filter expressed by the current tree selection. ``None`` means "no filter";
        otherwise the fully-expanded subtree set. See ``BrowserTopicTreeViewModel.expanded_filter_ids``.
        """
        return self._tree.expanded_filter_ids()

    def _on_cursor_changed(self) -> None:
        self._summary.set_topic_id(self._tree.cursor_topic_id)

    def _on_selection_changed(self) -> None:
        # No payload — listeners re-query ``current_filter``. The tree's SELECTION_CHANGED already
        # coalesces a cascade-toggle into a single fire, so this re-emit is also once-per-toggle.
        self.emit(self._filter_changed)
