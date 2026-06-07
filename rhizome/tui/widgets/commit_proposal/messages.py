from dataclasses import dataclass

from textual.message import Message


@dataclass
class SetTopicRequested(Message):
    """The user wants to assign a topic — to the cursor entry (``scope="current"``) or to every
    entry (``scope="all"``). Caught by ``CommitProposal`` which owns the topic-picker modal."""

    scope: str  # "current" | "all"
