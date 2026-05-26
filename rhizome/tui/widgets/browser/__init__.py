"""Browser widget (MVVM) — multi-tab data browser with a multi-select topic tree."""

from .knowledge_entry_tab import (
    DEFAULT_PAGE_LIMIT,
    KnowledgeEntryBrowserTabView,
    KnowledgeEntryBrowserTabViewModel,
)
from .tab_base import BrowserTabViewModel
from .topic_tree import BrowserTopicTreeView, BrowserTopicTreeViewModel
from .view import BrowserView
from .view_model import BrowserViewModel

__all__ = [
    "BrowserTabViewModel",
    "BrowserTopicTreeView",
    "BrowserTopicTreeViewModel",
    "BrowserView",
    "BrowserViewModel",
    "DEFAULT_PAGE_LIMIT",
    "KnowledgeEntryBrowserTabView",
    "KnowledgeEntryBrowserTabViewModel",
]
