"""CommitProposalInterruptVM — adapter that gives ``CommitProposalVM`` the chat-pane interrupt
surface (future-based resolution).

Subclasses both ``CommitProposalVM`` (core editing state machine) and ``InterruptVMBase`` (future
plumbing). Cooperative ``super().__init__()`` chains ensure ``ViewModelBase`` initialises exactly
once.

Resolution model: the VM auto-resolves its future when the lifecycle reaches ``DONE``, whether via
``accept_all()`` (resolves with the accepted entries + edit-instructions payload) or ``cancel()``
(resolves with ``accepted=None``, signalling the user rejected the proposal). Cancel does *not*
cancel the underlying ``asyncio.Future`` — like the flashcard interrupt, we resolve with a typed
payload so the caller can distinguish accept from cancel by inspecting the result rather than
catching ``CancelledError``.

Result shape::

    {
        "accepted": list[Entry] | None,   # None iff cancelled
        "edit_instructions": str,         # always present; empty if unused
    }
"""

from __future__ import annotations

from typing import Any

from rhizome.app.chat_pane.interrupts.base import InterruptVMBase
from rhizome.app.commit_proposal.commit_proposal import CommitProposalVM
from rhizome.app.commit_proposal.entry import Entry


class CommitProposalInterruptVM(CommitProposalVM, InterruptVMBase):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Interrupts are interactive feed entries — opt into the chat pane's ctrl+up/ctrl+down
        # navigation rotation.
        self.is_navigable = True
        # Watch our own state — when lifecycle lands in DONE, resolve the interrupt future. Both
        # accept_all and cancel emit dirty after transitioning state, so a single dirty subscriber
        # catches both.
        self.subscribe(self.dirty, self._maybe_resolve)

    def _maybe_resolve(self) -> None:
        if self.resolved:
            return
        if self.state != CommitProposalVM.State.DONE:
            return
        self.resolve(self._build_result(), remain_navigable=True)

    def _build_result(self) -> dict[str, Any]:
        accepted: list[Entry] | None
        if self.cancelled:
            accepted = None
        else:
            accepted = self.accepted_entries()
        return {
            "accepted": accepted,
            "edit_instructions": self.edit_instructions,
        }
