"""Commit-proposal view tree. Top-level export is ``CommitProposal``; the leaves
(``EntryList``, ``EntryDetails``, ``SharedTopicSetter``, ``CommitProposalChoices``, ``EditInstructions-
Area``) are public for testing but typically only the parent is mounted directly."""

from rhizome.tui.widgets.commit_proposal.choices import CommitProposalChoices
from rhizome.tui.widgets.commit_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.commit_proposal.entry_details import EntryDetails
from rhizome.tui.widgets.commit_proposal.entry_list import EntryList
from rhizome.tui.widgets.commit_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.commit_proposal.view import CommitProposal

__all__ = [
    "CommitProposal",
    "EditInstructionsArea",
    "EntryDetails",
    "EntryList",
    "CommitProposalChoices",
    "SharedTopicSetter",
]
