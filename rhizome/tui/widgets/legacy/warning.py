"""WarningChoices — widget for resolving dangerous-action confirmation interrupts.

Displays a warning icon and highlighted message, with Approve/Deny as default
options plus any additional options from the interrupt config. After selection,
the widget enters a collapsible state showing the question and chosen answer.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Button, Static

from .interrupt import InterruptWidgetBase

_DIM = "rgb(100,100,100)"


class WarningChoices(InterruptWidgetBase):
    """Displays a warning prompt with Approve / Deny and optional extra choices.

    Navigation: Up/Down to move highlight, Enter to select.
    After selection the widget enters a collapsible state that shows a summary
    of the question and chosen answer, expandable to the full prompt.
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Move up", show=False),
        Binding("down", "cursor_down", "Move down", show=False),
        Binding("enter", "select", "Select", show=False),
    ]

    DEFAULT_CSS = """
    WarningChoices {
        height: auto;
        layout: vertical;
        padding: 0 2;
        margin: 1 0;
    }
    WarningChoices .warning-icon {
        color: rgb(220, 160, 50);
    }
    WarningChoices .warning-message {
        color: rgb(220, 160, 50);
        margin-bottom: 1;
    }
    WarningChoices #warning-collapse {
        dock: right;
        width: auto;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: $text-muted;
        display: none;
    }
    WarningChoices #warning-collapse:hover {
        color: $text;
    }
    WarningChoices #warning-collapsed-summary {
        display: none;
    }
    """

    cursor: reactive[int] = reactive(0)

    def __init__(
        self,
        message: str = "The agent has requested a dangerous action.",
        options: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._message = message
        self._options = ["Approve", "Deny"] + (options or [])
        self._selected: str | None = None
        self._collapsed = False

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> WarningChoices:
        """Construct from an interrupt value dict."""
        return cls(
            message=value.get("message", "The agent has requested a dangerous action."),
            options=value.get("options"),
        )

    def compose(self) -> ComposeResult:
        yield Button("▼", id="warning-collapse")
        yield Static("⚠", classes="warning-icon")
        yield Static(self._message, classes="warning-message")
        yield Static(id="warning-options")
        yield Static("  (ctrl+c to cancel)", id="warning-hint")
        yield Static(id="warning-collapsed-summary")

    def on_mount(self) -> None:
        super().on_mount()
        self._render_options()
        self.query_one("#warning-hint", Static).styles.color = _DIM
        self.focus()
        self.scroll_visible(animate=False)
        self.call_after_refresh(self._render_options)

    def watch_cursor(self) -> None:
        self._render_options()

    def on_focus(self) -> None:
        super().on_focus()
        self._render_options()

    def on_blur(self) -> None:
        super().on_blur()
        self._render_options()

    def _render_options(self) -> None:
        focused = self.has_focus
        text = Text()
        for i, option in enumerate(self._options):
            if i > 0:
                text.append("\n")
            label = f"  {i + 1}. {option}"
            if not focused:
                text.append(label, style=_DIM)
            elif i == self.cursor:
                text.append(label, style="bold white")
            else:
                text.append(label, style=_DIM)
        self.query_one("#warning-options", Static).update(text)

    def action_cursor_up(self) -> None:
        if not self._future.done():
            self.cursor = (self.cursor - 1) % len(self._options)

    def action_cursor_down(self) -> None:
        if not self._future.done():
            self.cursor = (self.cursor + 1) % len(self._options)

    def action_select(self) -> None:
        if self._future.done():
            return
        selected = self._options[self.cursor]
        self._selected = selected
        self.resolve(selected)
        self._render_resolved()

    # ------------------------------------------------------------------
    # Collapse / expand (post-resolution)
    # ------------------------------------------------------------------

    def _build_summary_text(self) -> Text:
        """Build collapsed summary: '⚠ <message> → <selected>'."""
        summary = Text()
        summary.append("  ⚠ ", style="rgb(220,160,50)")
        summary.append(self._message, style=_DIM)
        summary.append(" → ", style=_DIM)
        summary.append(self._selected or "—", style="rgb(100,200,100)")
        return summary

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        btn = self.query_one("#warning-collapse", Button)
        btn.label = "▶" if collapsed else "▼"

        summary_widget = self.query_one("#warning-collapsed-summary", Static)
        icon = self.query_one(".warning-icon", Static)
        message = self.query_one(".warning-message", Static)
        options = self.query_one("#warning-options", Static)

        if collapsed:
            summary_widget.update(self._build_summary_text())
            summary_widget.display = True
            icon.display = False
            message.display = False
            options.display = False
        else:
            summary_widget.display = False
            icon.display = True
            message.display = True
            options.display = True
            self._render_resolved_options()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "warning-collapse":
            event.stop()
            self._set_collapsed(not self._collapsed)

    def _render_resolved_options(self) -> None:
        """Render options in read-only mode with the selected one highlighted."""
        text = Text()
        for i, option in enumerate(self._options):
            if i > 0:
                text.append("\n")
            label = f"  {i + 1}. {option}"
            if option == self._selected:
                text.append(label, style="rgb(100,200,100)")
            else:
                text.append(label, style=_DIM)
        self.query_one("#warning-options", Static).update(text)

    def _render_resolved(self) -> None:
        """Transition into collapsible resolved state."""
        self.query_one("#warning-hint", Static).update("")
        self.can_focus = True
        self.query_one("#warning-collapse", Button).display = True
        self._set_collapsed(True)
