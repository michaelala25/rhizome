"""FlashcardReviewInterrupt — view for ``FlashcardReviewInterruptVM``.

Trivial subclass of ``FlashcardReview`` — the interrupt semantics live entirely on the VM, which
auto-resolves its future when the session reaches DONE. This view exists so the type relationship
between the interrupt VM and its rendering is explicit (and so the typed ``self._vm`` carries
``InterruptVMBase`` surface for any future hooks).
"""

from __future__ import annotations

from rhizome.app.chat_pane.interrupts.flashcard_review import FlashcardReviewInterruptVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view
from rhizome.tui.widgets.flashcard_review.view import FlashcardReview


@register_feed_view(FlashcardReviewInterruptVM)
class FlashcardReviewInterrupt(FlashcardReview):
    _vm: FlashcardReviewInterruptVM  # type: ignore[assignment]
