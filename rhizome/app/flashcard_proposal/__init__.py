"""Flashcard-proposal VMs. Exposes ``Flashcard``, ``FlashcardProposalModel``, and
``FlashcardDetailsModel`` — the view tree under ``rhizome.tui.widgets.flashcard_proposal`` binds to
these."""

from rhizome.app.flashcard_proposal.flashcard import Flashcard
from rhizome.app.flashcard_proposal.flashcard_details import FlashcardDetailsModel
from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalModel

__all__ = [
    "Flashcard",
    "FlashcardDetailsModel",
    "FlashcardProposalModel",
]
