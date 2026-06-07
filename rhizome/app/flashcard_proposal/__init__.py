"""Flashcard-proposal VM. Exposes ``Flashcard`` and ``FlashcardProposalModel`` — the view tree
under ``rhizome.tui.widgets.flashcard_proposal`` binds to the model."""

from rhizome.app.flashcard_proposal.flashcard_proposal import Flashcard, FlashcardProposalModel

__all__ = [
    "Flashcard",
    "FlashcardProposalModel",
]
