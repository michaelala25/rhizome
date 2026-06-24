"""WarningChoices interrupt — Choices-style picker with amber warning styling.

Behaviourally identical to ``UserChoicesModel`` (intentionally duplicated rather than subclassed so
``isinstance`` dispatch order never matters). The only differences are the amber icon + summary
motif on the view side and ``from_interrupt`` always prepending Approve/Deny to caller-supplied
extras.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.app.chat_area.interrupts.base import InterruptModelBase

_AMBER = "rgb(220,160,50)"
_GREEN = "rgb(100,200,100)"
_DIM = "rgb(100,100,100)"


class WarningUserChoicesModel(InterruptModelBase):
    """Business logic for the WarningChoices interrupt. Identical surface to ``UserChoicesModel`` —
    deliberately not subclassed so that view dispatch by ``isinstance`` can check the two VMs in any
    order. ``from_interrupt`` prepends Approve/Deny to any caller-supplied extras.
    """

    DEFAULT_PROMPT = "The agent has requested a potentially dangerous action."

    def __init__(
        self,
        prompt: str = DEFAULT_PROMPT,
        options: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.is_navigable = True
        self.prompt = prompt
        self.options = list(options) if options else ["Approve", "Deny"]
        self.cursor: int = 0

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> WarningUserChoicesModel:
        # Caller-supplied ``options`` are treated as extras; Approve/Deny always come first.
        extras = list(value.get("options") or [])
        return cls(
            prompt=value.get("message", cls.DEFAULT_PROMPT),
            options=["Approve", "Deny"] + extras,
        )

    def move_cursor(self, delta: int) -> None:
        if self.resolved:
            return
        if not self.options:
            return
        new = (self.cursor + delta) % len(self.options)
        if new == self.cursor:
            return
        self.cursor = new
        self.emit(self.Callbacks.OnDirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        if not self.options:
            return
        self.resolve(self.options[self.cursor])
