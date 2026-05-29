"""Flashcard-proposal VMs. Exposes ``Flashcard``, ``FlashcardProposalVM``, and
``FlashcardDetailsVM`` — the view tree under ``rhizome.tui.widgets.flashcard_proposal`` binds to
these."""

from rhizome.app.flashcard_proposal.flashcard import Flashcard
from rhizome.app.flashcard_proposal.flashcard_details import FlashcardDetailsVM
from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalVM

__all__ = [
    "Flashcard",
    "FlashcardDetailsVM",
    "FlashcardProposalVM",
]
