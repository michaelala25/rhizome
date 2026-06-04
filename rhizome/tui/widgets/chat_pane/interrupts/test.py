"""TestInterrupt — view for the minimal routing-verification interrupt."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.app.chat_pane.interrupts.test import TestInterruptVM
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase


@register_feed_view(TestInterruptVM)
class TestInterrupt(NavigableFeedItemViewBase[TestInterruptVM]):
    """Single-Static projection of ``TestInterruptVM``. Up/Down move the cursor; Enter confirms. The
    widget keeps itself rendered after resolution so the conversational record shows what was chosen.
    """

    DEFAULT_CSS = """
    TestInterrupt {
        height: auto;
        padding: 1 2;
        margin: 0 2;
    }
    TestInterrupt.--resolved {
        color: $text-muted;
    }
    TestInterrupt #interrupt-body {
        width: 1fr;
        height: auto;
    }
    """

    BINDINGS = [
        Keybind.CursorUp.   as_binding("move_cursor(-1)", "Up",      show=False),
        Keybind.CursorDown. as_binding("move_cursor(1)",  "Down",    show=False),
        Keybind.MenuConfirm.as_binding("confirm",         "Confirm", show=True),
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
