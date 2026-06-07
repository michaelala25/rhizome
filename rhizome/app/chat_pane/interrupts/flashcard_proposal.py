"""``FlashcardProposalInterruptModel`` — adapter that gives ``FlashcardProposalModel`` the
chat-pane interrupt surface (future-based resolution).

Subclasses both ``FlashcardProposalModel`` (core editing state machine) and
``InterruptModelBase`` (future plumbing). Cooperative ``super().__init__()`` chains ensure
``ViewModelBase`` initialises exactly once.

Resolution model: the VM auto-resolves its future when the lifecycle reaches ``DONE``.
``accept()`` resolves with the accepted flashcards (no feedback), ``submit_revision(text)``
resolves with the accepted flashcards plus the user's feedback, ``cancel()`` resolves with
``accepted=None``. Cancel does *not* cancel the underlying ``asyncio.Future`` — we resolve with a
typed payload so the caller distinguishes accept / revise / cancel by inspecting the result
rather than catching ``CancelledError``.

Result shape::

    {
        "accepted": list[Flashcard] | None,   # None iff cancelled
        "edit_instructions": str,             # the revision feedback iff outcome is REVISED, else ""
    }
"""

from __future__ import annotations

from typing import Any

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.flashcard_proposal.flashcard_proposal import Flashcard, FlashcardProposalModel


class FlashcardProposalInterruptModel(FlashcardProposalModel, InterruptModelBase):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Interrupts are interactive feed entries — opt into the chat pane's ctrl+up/ctrl+down
        # navigation rotation.
        self.is_navigable = True
        # ``OnDone`` is fire-once on the REVIEWING → DONE transition, with the outcome as payload.
        # No state check needed.
        self.subscribe(self.Callbacks.OnDone, self._on_done)

    def _on_done(self, outcome: FlashcardProposalModel.Outcome) -> None:
        if self.resolved:
            return
        self.resolve(self._build_result(), remain_navigable=True)

    def _build_result(self) -> dict[str, Any]:
        accepted: list[Flashcard] | None
        if self.cancelled:
            accepted = None
        else:
            accepted = self.accepted_flashcards
        return {
            "accepted": accepted,
            "edit_instructions": self.revision_feedback or "",
        }
