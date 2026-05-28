"""Linked-flashcards sub-panel. Owned by ``EntryTabVM``; visible when the
parent tab is in ``State.LINKED_FLASHCARDS``. See ``CONTEXT.md``."""

from .view import LinkedFlashcardsPanel
from .view_model import LinkedFlashcardsPanelVM

__all__ = ["LinkedFlashcardsPanel", "LinkedFlashcardsPanelVM"]
