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

from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.app.chat_pane.interrupts.base import InterruptVMBase
from rhizome.app.chat_pane.interrupts.warning import WarningUserChoicesVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view

_AMBER = "rgb(220,160,50)"
_GREEN = "rgb(100,200,100)"
_DIM = "rgb(100,100,100)"


@register_feed_view(WarningUserChoicesVM)
class WarningUserChoices(NavigableFeedItemViewBase[WarningUserChoicesVM]):
    """Amber warning header + numbered options + cancel hint. On resolution collapses to a one-line
    ``⚠  <message> → <selected>`` summary. Ctrl+C cancels the pending future and is consumed in
    ``on_key`` to prevent the pane's priority cancel binding from firing. Border styling is
    inherited from ``NavigableFeedItem`` — the amber motif lives in the ⚠ icon + header text.
    """

    DEFAULT_CSS = """
    WarningUserChoices {
        height: auto;
        padding: 1 2;
        margin: 0 2;
    }
    WarningUserChoices.--resolved {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Keybind.CursorUp.    as_binding("move_cursor(-1)", "Up",      show=False),
        Keybind.CursorDown.  as_binding("move_cursor(1)",  "Down",    show=False),
        Keybind.MenuConfirm. as_binding("confirm",         "Confirm", show=True),
        Keybind.DialogCancel.as_binding("cancel",          "Cancel",  show=True),
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
