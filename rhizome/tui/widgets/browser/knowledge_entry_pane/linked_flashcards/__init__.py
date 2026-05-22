"""Linked-flashcards sub-pane (MVVM). Owned by ``KnowledgeEntryBrowserPaneViewModel``; rendered when
the parent pane is in ``State.LINKED_FLASHCARDS``."""

from .view import LinkedFlashcardsPaneView
from .view_model import LinkedFlashcardsPaneViewModel

__all__ = ["LinkedFlashcardsPaneView", "LinkedFlashcardsPaneViewModel"]
