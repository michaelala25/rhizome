"""Knowledge-entry browser tab (MVVM)."""

from .view import KnowledgeEntryBrowserTabView
from .view_model import DEFAULT_PAGE_LIMIT, KnowledgeEntryBrowserTabViewModel

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "KnowledgeEntryBrowserTabView",
    "KnowledgeEntryBrowserTabViewModel",
]
