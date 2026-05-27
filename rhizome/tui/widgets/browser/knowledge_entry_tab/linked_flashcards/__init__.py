"""Linked-flashcards sub-panel. Owned by ``KnowledgeEntryBrowserTabViewModel``; visible when the
parent tab is in ``State.LINKED_FLASHCARDS``. See ``CONTEXT.md``."""

from .view import LinkedFlashcardsPanelView
from .view_model import LinkedFlashcardsPanelViewModel

__all__ = ["LinkedFlashcardsPanelView", "LinkedFlashcardsPanelViewModel"]
