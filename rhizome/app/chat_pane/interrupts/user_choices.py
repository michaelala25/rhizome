"""Choices interrupt — MVVM port of the legacy ``widgets/choices.py``.

A feed-resident interrupt that prompts the user with a numbered list of options. Up/Down move a
cursor, Enter resolves the VM's future with the selected option *string* (matching the legacy
contract). The chat pane awaits ``vm.future()`` to discover the user's selection.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase


class UserChoicesModel(InterruptModelBase):
    """Business logic for the Choices interrupt: prompt + options + cursor + resolution.

    Mutators are idempotent on the resolved state — stale key handlers can't double-fire the future.
    ``from_interrupt`` matches the legacy classmethod's contract for constructing from an agent
    interrupt value dict.
    """

    DEFAULT_PROMPT = "The agent requires your input:"
    DEFAULT_OPTIONS: tuple[str, ...] = ("Continue", "Cancel")

    def __init__(
        self,
        prompt: str = DEFAULT_PROMPT,
        options: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.is_navigable = True
        self.prompt = prompt
        self.options = list(options) if options else list(self.DEFAULT_OPTIONS)
        self.cursor: int = 0

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> UserChoicesModel:
        """Build a VM from an agent interrupt value dict (legacy contract)."""
        return cls(
            prompt=value.get("message", cls.DEFAULT_PROMPT),
            options=value.get("options"),
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


_DIM = "rgb(100,100,100)"
_GREEN = "rgb(100,200,100)"
