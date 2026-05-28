"""WarningChoices interrupt — Choices-style picker with amber warning styling.

Behaviourally identical to ``UserChoicesVM`` (intentionally duplicated rather than subclassed so
``isinstance`` dispatch order never matters). The only differences are the amber icon + summary
motif on the view side and ``from_interrupt`` always prepending Approve/Deny to caller-supplied
extras.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from ..view_base import ViewBase
from .interrupt import InterruptVMBase

_AMBER = "rgb(220,160,50)"
_GREEN = "rgb(100,200,100)"
_DIM = "rgb(100,100,100)"


class WarningUserChoicesVM(InterruptVMBase):
    """Business logic for the WarningChoices interrupt. Identical surface to ``UserChoicesVM`` —
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
    def from_interrupt(cls, value: dict[str, Any]) -> WarningUserChoicesVM:
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
        self.emit(self.dirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        if not self.options:
            return
        self.resolve(self.options[self.cursor])


class WarningUserChoices(ViewBase[WarningUserChoicesVM]):
    """Amber warning header + numbered options + cancel hint. On resolution collapses to a one-line
    ``⚠  <message> → <selected>`` summary. Ctrl+C cancels the pending future and is consumed in
    ``on_key`` to prevent the pane's priority cancel binding from firing.
    """

    DEFAULT_CSS = """
    WarningUserChoices {
        height: auto;
        padding: 1 2;
        margin: 0 2;
        border: round rgb(120,90,30);
    }
    WarningUserChoices:focus {
        border: round rgb(220,160,50);
    }
    WarningUserChoices.--resolved {
        border: round rgb(60,50,30);
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
        ("enter", "confirm", "Confirm"),
        ("ctrl+c", "cancel", "Cancel"),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("", id="warning-header")
        yield Static("", id="warning-options")
        yield Static("", id="warning-hint")
        yield Static("", id="warning-summary")

    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    def _refresh(self) -> None:
        header = self.query_one("#warning-header", Static)
        options = self.query_one("#warning-options", Static)
        hint = self.query_one("#warning-hint", Static)
        summary = self.query_one("#warning-summary", Static)

        if self._vm.resolved:
            header.display = False
            options.display = False
            hint.display = False
            summary.display = True
            summary.update(self._build_summary())
            self.add_class("--resolved")
            return

        header.display = True
        options.display = True
        hint.display = True
        summary.display = False

        header_text = Text()
        header_text.append("⚠  ", style=_AMBER)
        header_text.append(self._vm.prompt, style=_AMBER)
        header.update(header_text)

        options.update(self._build_options())
        hint.update(Text("\n  (ctrl+c to cancel)", style=_DIM))

    def _build_options(self) -> Text:
        text = Text()
        for i, option in enumerate(self._vm.options):
            text.append("\n")
            label = f"  {i + 1}. {option}"
            if i == self._vm.cursor:
                text.append(label, style="bold white")
            else:
                text.append(label, style=_DIM)
        return text

    def _build_summary(self) -> Text:
        summary = Text()
        summary.append("⚠  ", style=_AMBER)
        summary.append(self._vm.prompt, style=_DIM)
        if self._vm.cancelled:
            summary.append("  —  ", style=_DIM)
            summary.append("cancelled", style=_DIM)
        else:
            summary.append("  →  ", style=_DIM)
            summary.append(str(self._vm.result), style=_GREEN)
        return summary

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()

    def action_cancel(self) -> None:
        self._vm.cancel()
