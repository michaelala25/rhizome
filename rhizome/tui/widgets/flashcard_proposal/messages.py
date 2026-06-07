from dataclasses import dataclass

from textual.message import Message


@dataclass
class SetTopicRequested(Message):
    """The user wants to assign a topic — to the cursor flashcard (``scope="current"``) or to
    every flashcard (``scope="all"``). Caught by ``FlashcardProposal`` which owns the topic-picker
    modal."""

    scope: str  # "current" | "all"
