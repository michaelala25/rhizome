"""Flashcard-proposal view tree. Top-level export is ``FlashcardProposal``; the leaves
(``FlashcardList``, ``FlashcardDetails``, ``SharedTopicSetter``, ``FlashcardProposalChoices``,
``EditInstructionsArea``) are public for testing but typically only the parent is mounted
directly."""

from rhizome.tui.widgets.flashcard_proposal.choices import FlashcardProposalChoices
from rhizome.tui.widgets.flashcard_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.flashcard_proposal.flashcard_details import FlashcardDetails
from rhizome.tui.widgets.flashcard_proposal.flashcard_list import FlashcardList
from rhizome.tui.widgets.flashcard_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.flashcard_proposal.view import FlashcardProposal

__all__ = [
    "EditInstructionsArea",
    "FlashcardDetails",
    "FlashcardList",
    "FlashcardProposal",
    "FlashcardProposalChoices",
    "SharedTopicSetter",
]
