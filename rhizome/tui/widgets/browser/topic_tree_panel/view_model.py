"""TopicTreePanelViewModel — bundles the topic tree + topic-details panel as the browser's
left-rail VM.

Internal wiring: ``tree.cursor_changed`` → ``details.set_topic_id``. Exposes ``current_filter`` as
a composite read so callers don't have to know the filter is derived from the tree.
"""

from __future__ import annotations

from typing import Any

from rhizome.logs import get_logger

from ..topic_tree import BrowserTopicTreeViewModel
from rhizome.app.vm import ViewModelBase
from .topic_details import TopicDetailsViewModel

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanelViewModel(ViewModelBase):
    """Owns the tree + details child VMs and exposes the panel-level ``current_filter``."""

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)
        self._details = TopicDetailsViewModel(session_factory)

        self._tree.subscribe(self._tree.cursor_changed, self._on_cursor_changed)

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    @property
    def details(self) -> TopicDetailsViewModel:
        return self._details

    @property
    def current_filter(self) -> frozenset[int] | None:
        """Topic-id filter expressed by the current tree selection. ``None`` means "no filter";
        otherwise the fully-expanded subtree set. See ``BrowserTopicTreeViewModel.expanded_filter_ids``.
        """
        return self._tree.expanded_filter_ids()

    def _on_cursor_changed(self) -> None:
        self._details.set_topic_id(self._tree.cursor_topic_id)
