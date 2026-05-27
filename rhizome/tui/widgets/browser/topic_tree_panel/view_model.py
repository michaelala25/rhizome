"""TopicTreePanelViewModel — bundles tree + summary as the browser's left-rail VM.

Internal wiring: ``tree.cursor_changed`` → ``summary.set_topic_id``. Selection changes propagate
to the orchestrator through ``tree.selection_changed`` directly — the panel does not re-emit it.
``current_filter`` is exposed here as a composite read so callers don't have to know the filter is
derived from the tree.
"""

from __future__ import annotations

from typing import Any

from rhizome.logs import get_logger

from ..topic_summary import TopicSummaryViewModel
from ..topic_tree import BrowserTopicTreeViewModel
from ...view_model_base import ViewModelBase

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanelViewModel(ViewModelBase):
    """Owns the tree + summary child VMs and exposes a panel-level ``current_filter`` read."""

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)
        self._summary = TopicSummaryViewModel(session_factory)

        self._tree.subscribe(self._tree.cursor_changed, self._on_cursor_changed)

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    @property
    def summary(self) -> TopicSummaryViewModel:
        return self._summary

    @property
    def current_filter(self) -> frozenset[int] | None:
        """Topic-id filter expressed by the current tree selection. ``None`` means "no filter";
        otherwise the fully-expanded subtree set. See ``BrowserTopicTreeViewModel.expanded_filter_ids``.
        """
        return self._tree.expanded_filter_ids()

    def _on_cursor_changed(self) -> None:
        self._summary.set_topic_id(self._tree.cursor_topic_id)
