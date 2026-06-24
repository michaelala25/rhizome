"""CommitProposalInterrupt — view for ``CommitProposalInterruptModel``.

Trivial subclass of ``CommitProposal`` — the interrupt semantics live entirely on the VM, which
auto-resolves its future when the lifecycle reaches DONE. This view exists so the type relation
between the interrupt VM and its rendering is explicit (and so the typed ``self._vm`` carries
``InterruptModelBase`` surface for any future hooks).
"""

from __future__ import annotations

from rhizome.app.chat_area.interrupts.commit_proposal import CommitProposalInterruptModel
from rhizome.tui.widgets.chat_area.feed_registry import register_feed_view
from rhizome.tui.widgets.commit_proposal.commit_proposal import CommitProposal


@register_feed_view(CommitProposalInterruptModel)
class CommitProposalInterrupt(CommitProposal):
    _vm: CommitProposalInterruptModel  # type: ignore[assignment]
