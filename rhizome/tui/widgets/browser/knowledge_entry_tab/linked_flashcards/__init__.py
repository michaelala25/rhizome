"""Linked-flashcards sub-tab (MVVM). Owned by ``KnowledgeEntryBrowserTabViewModel``; rendered when
the parent tab is in ``State.LINKED_FLASHCARDS``."""

from .view import LinkedFlashcardsPanelView
from .view_model import LinkedFlashcardsPanelViewModel

__all__ = ["LinkedFlashcardsPanelView", "LinkedFlashcardsPanelViewModel"]
