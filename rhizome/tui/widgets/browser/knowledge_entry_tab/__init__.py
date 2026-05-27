"""Knowledge-entry browser tab — paginated DataTable with details / linked-flashcards panes."""

from .view import KnowledgeEntryBrowserTabView
from .view_model import DEFAULT_PAGE_LIMIT, KnowledgeEntryBrowserTabViewModel

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "KnowledgeEntryBrowserTabView",
    "KnowledgeEntryBrowserTabViewModel",
]
