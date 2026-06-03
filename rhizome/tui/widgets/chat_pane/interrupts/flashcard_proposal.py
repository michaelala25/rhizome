"""``FlashcardProposalInterrupt`` — view for ``FlashcardProposalInterruptVM``.

Trivial subclass of ``FlashcardProposal`` — the interrupt semantics live entirely on the VM,
which auto-resolves its future when the lifecycle reaches DONE. This view exists so the type
relation between the interrupt VM and its rendering is explicit (and so the typed ``self._vm``
carries ``InterruptVMBase`` surface for any future hooks).
"""

from __future__ import annotations

from rhizome.app.chat_pane.interrupts.flashcard_proposal import FlashcardProposalInterruptVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view
from rhizome.tui.widgets.flashcard_proposal.view import FlashcardProposal


@register_feed_view(FlashcardProposalInterruptVM)
class FlashcardProposalInterrupt(FlashcardProposal):
    _vm: FlashcardProposalInterruptVM  # type: ignore[assignment]
