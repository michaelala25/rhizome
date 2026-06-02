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

from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.app.chat_pane.interrupts.base import InterruptVMBase
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesVM


class UserChoices(NavigableFeedItemViewBase[UserChoicesVM]):
    """Multi-Static projection of ``UserChoicesVM``: prompt header, numbered options with cursor
    marker, ctrl+c hint, and a post-resolution summary line. On resolve the prompt/options/hint hide
    and the summary takes over (``prompt → selected`` or ``prompt — cancelled``).
    """

    DEFAULT_CSS = """
    UserChoices {
        height: auto;
        padding: 1 2;
        margin: 0 2;
    }
    UserChoices.--resolved {
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
