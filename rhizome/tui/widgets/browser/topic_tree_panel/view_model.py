"""TopicTreePanelViewModel — wraps the topic tree as the browser's left-rail VM.

Exposes ``current_filter`` as a composite read so callers don't have to know the filter is derived
from the tree. Today this is a thin wrapper; future dialogs (rename, create, delete) will hang
their own child VMs off this panel.
"""

from __future__ import annotations

from typing import Any

from rhizome.logs import get_logger

from ..topic_tree import BrowserTopicTreeViewModel
from ...view_model_base import ViewModelBase

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanelViewModel(ViewModelBase):
    """Owns the tree child VM and exposes the panel-level ``current_filter``."""

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = BrowserTopicTreeViewModel(session_factory)

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    @property
    def current_filter(self) -> frozenset[int] | None:
        """Topic-id filter expressed by the current tree selection. ``None`` means "no filter";
        otherwise the fully-expanded subtree set. See ``BrowserTopicTreeViewModel.expanded_filter_ids``.
        """
        return self._tree.expanded_filter_ids()
