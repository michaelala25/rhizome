"""FlashcardReviewInterruptModel — adapter that gives ``FlashcardReviewModel`` the chat-pane interrupt
surface (future-based resolution).

Subclasses both ``FlashcardReviewModel`` (core review state machine) and ``InterruptModelBase`` (future
plumbing). Cooperative ``super().__init__()`` chains ensure ``ViewModelBase`` initialises exactly
once.

Resolution model: the VM auto-resolves its future when the session reaches ``DONE`` (whether via
natural completion or user cancellation). The result payload mirrors what the legacy widget
produced — a ``{completed: bool, cards: [...]}`` dict consumed by the agent's review tool. Cancel
*does not* cancel the underlying ``asyncio.Future``; it resolves with ``completed=False`` so the
caller can distinguish partial state from a true abort.
"""

from __future__ import annotations

from typing import Any

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.flashcard_review.flashcard import Flashcard
from rhizome.app.flashcard_review.review import FlashcardReviewModel


class FlashcardReviewInterruptModel(FlashcardReviewModel, InterruptModelBase):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Watch our own state — when the session lands in DONE, resolve the interrupt future. The
        # finish/cancel paths both emit dirty after transitioning state, so a single dirty subscriber
        # catches both.
        self.subscribe(self.Callbacks.OnDirty, self._maybe_resolve)

    def _maybe_resolve(self) -> None:
        if self.resolved:
            return
        if self.state != FlashcardReviewModel.State.DONE:
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
