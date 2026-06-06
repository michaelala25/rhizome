"""TestInterruptModel — minimal interrupt for routing verification."""

from __future__ import annotations

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase


class TestInterruptModel(InterruptModelBase):
    """Minimal interrupt: a cursor over a list of options, Enter resolves with the selected option's
    value. Built for routing verification — not linked to any tool call.
    """

    def __init__(self, prompt: str = "Choose one:", options: list[str] | None = None) -> None:
        super().__init__()
        self.prompt = prompt
        self.options = options or ["ok", "cancel"]
        self.cursor: int = 0

    def move_cursor(self, delta: int) -> None:
        if self.resolved:
            return
        new = (self.cursor + delta) % len(self.options)
        if new == self.cursor:
            return
        self.cursor = new
        self.emit(self.Callbacks.OnDirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        self.resolve(self.options[self.cursor])
