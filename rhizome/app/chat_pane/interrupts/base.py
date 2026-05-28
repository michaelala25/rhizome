"""InterruptVMBase — common machinery for any feed-resident interrupt VM.

Interrupts are feed-native sub-VMs: they live as siblings of AgentMessage in the chat pane's feed,
not as children of it. The peek-tail rule means an interrupt appearing implicitly closes the open
AgentMessage (since the feed tail is no longer one), and the next agent chunk opens a fresh one.

The future lives on the VM, not on the widget. ``present_interrupt`` on the chat pane awaits
``vm.wait_for_selection()``; view-side mutator calls (``move_cursor``, ``confirm``) eventually trigger
``resolve(value)``, which sets the future. This survives view re-mount, decouples the awaiter from
Textual, and keeps the interrupt unit-testable without a Textual app.

Mutators are idempotent on the resolved state: stale key handlers can't double-fire the future, and
``cancel`` after ``resolve`` is a no-op.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rhizome.app.vm import ViewModelBase


class InterruptVMBase(ViewModelBase):
    """Common machinery for any feed-resident interrupt VM. Subclasses define the interaction surface
    (cursor, options, etc.); the base owns the future plumbing + idempotency guards.
    """

    def __init__(self) -> None:
        super().__init__()
        self._future: asyncio.Future[Any] = asyncio.Future()
        self.resolved: bool = False
        self.cancelled: bool = False
        self.result: Any | None = None

    async def future(self) -> Any:
        """Block until the user resolves (or cancels) this interrupt. Returns the resolved value, or
        raises ``asyncio.CancelledError`` on cancel.
        """
        return await self._future

    def resolve(self, value: Any) -> None:
        """Set the future to ``value``. No-op if already resolved/cancelled."""
        if self.resolved:
            return

        self.resolved = True
        self.is_navigable = False
        self.result = value
        if not self._future.done():
            self._future.set_result(value)

        self.emit(self.dirty)

    def cancel(self) -> None:
        """Cancel the future (the awaiter sees ``CancelledError``). No-op if already resolved or
        cancelled."""
        if self.resolved:
            return

        self.resolved = True
        self.is_navigable = False
        self.cancelled = True
        if not self._future.done():
            self._future.cancel()

        self.emit(self.dirty)
