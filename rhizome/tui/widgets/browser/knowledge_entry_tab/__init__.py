"""Knowledge-entry browser tab — paginated DataTable with details / linked-flashcards panes."""

from .view import EntryTab
from .view_model import DEFAULT_PAGE_LIMIT, EntryTabVM

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "EntryTab",
    "EntryTabVM",
]
