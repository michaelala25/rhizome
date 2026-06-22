"""FlashcardReviewInterruptModel — adapter that gives ``FlashcardReviewModel`` the chat-pane interrupt
surface (future-based resolution).

Subclasses both ``FlashcardReviewModel`` (core review state machine) and ``InterruptModelBase`` (future
plumbing). Cooperative ``super().__init__()`` chains ensure ``ViewModelBase`` initialises exactly
once.

Resolution model: the VM auto-resolves its future when the session reaches ``DONE`` (whether via
natural completion or user cancellation). The ``OnLifecycle`` subscription fires on every state
transition; this adapter resolves on the DONE one. The result payload mirrors what the legacy
widget produced — a ``{completed: bool, cards: [...]}`` dict consumed by the agent's review tool.
Cancel *does not* cancel the underlying ``asyncio.Future``; it resolves with ``completed=False`` so
the caller can distinguish partial state from a true abort.
"""

from __future__ import annotations

from typing import Any

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.flashcard_review.flashcard import Flashcard
from rhizome.app.flashcard_review.flashcard_review import FlashcardReviewModel


class FlashcardReviewInterruptModel(FlashcardReviewModel, InterruptModelBase):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Watch our own state — when the session lands in DONE, resolve the interrupt future. Both the
        # finish and cancel paths emit OnLifecycle with the new state as payload, so this single
        # subscriber catches both.
        self.subscribe(self.Callbacks.OnLifecycle, self._on_lifecycle)

    @classmethod
    def from_interrupt(cls, value: dict[str, Any], context: Any) -> "FlashcardReviewInterruptModel":
        """Build the review VM from a ``flashcard_review`` interrupt value plus the run context.

        ``value["cards"]`` already arrives in ``FlashcardData`` shape (the review tool builds it that
        way), so it threads straight through. The DB ``session_factory`` (for the post-session FSRS
        commit) and the agent ``runtime`` (the auto-scorer's handle — it mints a ``flashcard_scorer``
        session per batch) are pulled off the context the stream hands the interrupt.
        """
        return cls(
            cards=value["cards"],
            session_factory=context.session_factory,
            auto_score_enabled=value.get("auto_score_enabled", False),
            agent_runtime=context.runtime,
        )

    def _on_lifecycle(self, state: FlashcardReviewModel.State) -> None:
        if self.resolved:
            return
        if state != FlashcardReviewModel.State.DONE:
            return
        self.resolve(self._build_result(), remain_navigable=True)

    def _build_result(self) -> dict[str, Any]:
        cards = []
        for card in self._cards:
            score_val: int | None = None
            score_label: str | None = None
            score = card.score

            if score in (
                Flashcard.Score.AGAIN,
                Flashcard.Score.HARD,
                Flashcard.Score.GOOD,
                Flashcard.Score.EASY,
            ):
                score_val = score.value
                score_label = score.name.lower()
            elif score == Flashcard.Score.SKIPPED:
                score_label = "skipped"
            elif score == Flashcard.Score.AUTO:
                # Session ended while the card was still pending a batch (e.g. user cancelled
                # mid-batch). No final rating.
                score_label = "auto"

            cards.append({
                "id": card.id,
                "question": card.question,
                "answer": card.answer,
                "user_answer": card.user_answer or "",
                "score": score_val,
                "score_label": score_label,
                "flagged": card.flagged,
                "duration": round(card.elapsed_time, 1),
                # Final in-memory FSRS state. Consumer (the review tool) decides whether to persist.
                "fsrs_card": card.fsrs_card,
            })

        return {
            "completed": not self.cancelled,
            "cards": cards,
        }
