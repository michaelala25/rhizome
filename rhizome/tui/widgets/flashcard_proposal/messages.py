"""Inter-widget messages used by the flashcard-proposal view tree.

Mirrors ``rhizome.tui.widgets.commit_proposal.messages``. Most cross-widget communication is
handled by Textual's native key-event bubbling — leaves whose bindings are disabled (via
``check_action``) or that simply don't bind a key let the event flow up to the parent
``FlashcardProposal``'s own bindings. The one message that survives is the topic-picker request,
because the modal lives on the parent (it owns the ``session_factory``) and the leaves need a
way to ask for it.
"""

from __future__ import annotations

from typing import Literal

from textual.message import Message


class SetTopicRequested(Message):
    """Request that the parent open ``TopicSelectorScreen`` and apply the result.

    ``scope`` is ``"current"`` (apply to the cursor flashcard via
    ``set_current_flashcard_topic``) or ``"all"`` (apply to every flashcard via
    ``set_topic_all``).
    """

    def __init__(self, scope: Literal["current", "all"]) -> None:
        super().__init__()
        self.scope = scope
