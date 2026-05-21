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

from ..view_base import ViewBase
from .interrupt import InterruptViewModelBase


class ChoicesViewModel(InterruptViewModelBase):
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
    def from_interrupt(cls, value: dict[str, Any]) -> ChoicesViewModel:
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
        self.emit(self.dirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        if not self.options:
            return
        self.resolve(self.options[self.cursor])


_DIM = "rgb(100,100,100)"
_GREEN = "rgb(100,200,100)"


class ChoicesView(ViewBase[ChoicesViewModel]):
    """Multi-Static projection of ``ChoicesViewModel``: prompt header, numbered options with cursor
    marker, ctrl+c hint, and a post-resolution summary line. On resolve the prompt/options/hint hide
    and the summary takes over (``prompt → selected`` or ``prompt — cancelled``).
    """

    DEFAULT_CSS = """
    ChoicesView {
        height: auto;
        padding: 1 2;
        margin: 0 2;
    }
    ChoicesView.--resolved {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("up", "move_cursor(-1)", "Up"),
        Binding("down", "move_cursor(1)", "Down"),
        Binding("enter", "confirm", "Confirm"),
        Binding("ctrl+c", "cancel", "Cancel"),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("", id="choices-header")
        yield Static("", id="choices-options")
        yield Static("", id="choices-hint")
        yield Static("", id="choices-summary")

    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    def _refresh(self) -> None:
        header = self.query_one("#choices-header", Static)
        options = self.query_one("#choices-options", Static)
        hint = self.query_one("#choices-hint", Static)
        summary = self.query_one("#choices-summary", Static)

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

        header.update(self._vm.prompt)
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
