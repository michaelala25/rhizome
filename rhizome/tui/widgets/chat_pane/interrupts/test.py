"""TestInterrupt — view for the minimal routing-verification interrupt."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.app.chat_pane.interrupts.test import TestInterruptVM
from rhizome.tui.widgets.view_base import ViewBase


class TestInterrupt(ViewBase[TestInterruptVM]):
    """Single-Static projection of ``TestInterruptVM``. Up/Down move the cursor; Enter confirms. The
    widget keeps itself rendered after resolution so the conversational record shows what was chosen.
    """

    DEFAULT_CSS = """
    TestInterrupt {
        height: auto;
        padding: 1 2;
        margin: 0 2;
        border: round rgb(80, 80, 80);
    }
    TestInterrupt:focus {
        border: round rgb(140, 140, 200);
    }
    TestInterrupt.--resolved {
        border: round rgb(50, 50, 50);
        color: $text-muted;
    }
    TestInterrupt #interrupt-body {
        width: 1fr;
        height: auto;
    }
    """

    BINDINGS = [
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
        ("enter", "confirm", "Confirm"),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("", id="interrupt-body")

    def on_mount(self) -> None:
        # Take focus on appearance so keys route here without needing a click.
        self.focus()
        self._refresh()

    def _refresh(self) -> None:
        lines: list[str] = [self._vm.prompt, ""]

        for i, opt in enumerate(self._vm.options):
            marker = ">" if (i == self._vm.cursor and not self._vm.resolved) else " "
            lines.append(f"{marker} {opt}")

        if self._vm.resolved:
            tail = "(cancelled)" if self._vm.cancelled else f"chose: {self._vm.result}"
            lines.extend(["", tail])
            self.add_class("--resolved")

        self.query_one("#interrupt-body", Static).update("\n".join(lines))

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()
