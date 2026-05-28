"""MultipleChoices — multi-question interrupt widget with tabbed navigation.

Presents multiple questions as a horizontal tab bar with checkboxes. The user
answers each question by selecting from a choices list (identical styling to
InterruptChoices). Ctrl+Left/Right navigates between questions. After all
questions are answered, Ctrl+Enter triggers a "Submit answers?" confirmation.
Ctrl+C cancels the entire widget at any point.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Button, Static

from .interrupt import InterruptWidgetBase

_DIM = "rgb(100,100,100)"


class _Phase(Enum):
    """Internal state machine for the widget."""

    ANSWERING = auto()
    CONFIRMING = auto()


class MultipleChoices(InterruptWidgetBase):
    """Multi-question interrupt widget with tabbed question navigation.

    Each question is displayed as a tab in a horizontal bar with a checkbox
    indicator.  The user navigates questions with Ctrl+Left/Right and selects
    options with Up/Down/Enter — identical to InterruptChoices.

    After all questions are answered, Ctrl+Enter shows a "Submit answers?"
    confirmation.  Selecting "Yes" resolves the future with a dict mapping
    question names to selected options.  Selecting "No" returns to the
    answering phase.
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Move up", show=False),
        Binding("down", "cursor_down", "Move down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("ctrl+left", "prev_question", "Previous question", show=False),
        Binding("ctrl+right", "next_question", "Next question", show=False),
        Binding("ctrl+j", "submit", "Submit answers", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    MultipleChoices {
        height: auto;
        layout: vertical;
        padding: 0 2;
        margin: 1 0;
    }
    MultipleChoices #mc-hint {
        margin-bottom: 1;
    }
    MultipleChoices #mc-collapse {
        dock: right;
        width: auto;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: $text-muted;
        display: none;
    }
    MultipleChoices #mc-collapse:hover {
        color: $text;
    }
    MultipleChoices #mc-collapsed-summary {
        display: none;
    }
    """

    active_question: reactive[int] = reactive(0)
    cursor: reactive[int] = reactive(0)

    def __init__(
        self,
        questions: list[dict[str, Any]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        # Each question: {"name": str, "prompt": str, "options": list[str]}
        self._questions = questions
        self._answers: dict[int, str] = {}  # question index -> selected option
        self._cursors: dict[int, int] = {}  # question index -> cursor position
        self._phase = _Phase.ANSWERING
        self._confirm_cursor = 0
        self._has_confirmed_once = False
        self._collapsed = False

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> MultipleChoices:
        """Construct from an interrupt value dict.

        Expected format::

            {
                "type": "multiple_choice",
                "questions": [
                    {"name": "Tab Name", "prompt": "Full question?", "options": ["A", "B"]},
                    ...
                ]
            }
        """
        return cls(questions=value["questions"])

    # ------------------------------------------------------------------
    # Compose & mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Button("▼", id="mc-collapse")
        yield Static(id="mc-tabs")
        yield Static(id="mc-hint")
        yield Static(id="mc-prompt")
        yield Static(id="mc-options")
        yield Static(id="mc-collapsed-summary")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#mc-hint", Static).styles.color = _DIM
        self._render_all()
        self.focus()
        self.scroll_visible(animate=False)
        self.call_after_refresh(self._render_all)

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_active_question(self) -> None:
        # Restore per-question cursor
        self.cursor = self._cursors.get(self.active_question, 0)
        self._render_all()

    def watch_cursor(self) -> None:
        if self._phase == _Phase.ANSWERING:
            self._cursors[self.active_question] = self.cursor
        self._render_options()

    def on_focus(self) -> None:
        super().on_focus()
        self._render_all()

    def on_blur(self) -> None:
        super().on_blur()
        self._render_all()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_all(self) -> None:
        self._render_tabs()
        self._render_hint()
        self._render_prompt()
        self._render_options()

    def _render_tabs(self) -> None:
        text = Text()
        for i, q in enumerate(self._questions):
            if i > 0:
                text.append("  ")
            checked = "x" if i in self._answers else " "
            label = f"[{checked}] {q['name']}"
            if self._phase == _Phase.ANSWERING and i == self.active_question:
                text.append(label, style="bold white")
            elif i in self._answers:
                text.append(label, style="rgb(100,200,100)")
            else:
                text.append(label, style="rgb(100,100,100)")
        self.query_one("#mc-tabs", Static).update(text)

    def _render_hint(self) -> None:
        if self._phase == _Phase.CONFIRMING:
            hint = "  (ctrl+c to cancel)"
        else:
            all_answered = len(self._answers) == len(self._questions)
            if all_answered:
                hint = "  (ctrl+left/right to navigate, ctrl+enter to submit, ctrl+c to cancel)"
            else:
                hint = "  (ctrl+left/right to navigate between questions, ctrl+c to cancel)"
        self.query_one("#mc-hint", Static).update(hint)

    def _render_prompt(self) -> None:
        if self._phase == _Phase.CONFIRMING:
            self.query_one("#mc-prompt", Static).update("Submit answers?")
        else:
            q = self._questions[self.active_question]
            self.query_one("#mc-prompt", Static).update(q["prompt"])

    def _render_options(self) -> None:
        focused = self.has_focus

        if self._phase == _Phase.CONFIRMING:
            options = ["Yes", "No"]
            cursor = self._confirm_cursor
        else:
            options = self._questions[self.active_question]["options"]
            cursor = self.cursor

        # Determine which option is the "answered" one for this question
        answered_option = None
        if self._phase == _Phase.ANSWERING and self.active_question in self._answers:
            answered_option = self._answers[self.active_question]

        text = Text()
        for i, option in enumerate(options):
            if i > 0:
                text.append("\n")
            label = f"  {i + 1}. {option}"
            if not focused:
                text.append(label, style="rgb(100,100,100)")
            elif i == cursor:
                text.append(label, style="bold white")
            elif option == answered_option:
                text.append(label, style="rgb(100,200,100)")
            else:
                text.append(label, style="rgb(100,100,100)")
        self.query_one("#mc-options", Static).update(text)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self._future.done():
            return
        if self._phase == _Phase.CONFIRMING:
            self._confirm_cursor = (self._confirm_cursor - 1) % 2
            self._render_options()
        else:
            n = len(self._questions[self.active_question]["options"])
            self.cursor = (self.cursor - 1) % n

    def action_cursor_down(self) -> None:
        if self._future.done():
            return
        if self._phase == _Phase.CONFIRMING:
            self._confirm_cursor = (self._confirm_cursor + 1) % 2
            self._render_options()
        else:
            n = len(self._questions[self.active_question]["options"])
            self.cursor = (self.cursor + 1) % n

    def action_select(self) -> None:
        if self._future.done():
            return

        if self._phase == _Phase.CONFIRMING:
            if self._confirm_cursor == 0:  # Yes
                # Resolve with answers dict
                result = {
                    self._questions[i]["name"]: answer
                    for i, answer in self._answers.items()
                }
                self.resolve(result)
                self._show_final_summary()
            else:  # No
                self._has_confirmed_once = True
                self._phase = _Phase.ANSWERING
                self._render_all()
            return

        # Answering phase: record answer
        q = self._questions[self.active_question]
        selected = q["options"][self.cursor]
        self._answers[self.active_question] = selected

        # Auto-advance to next unanswered question, or go to confirmation
        next_q = self._find_next_unanswered()
        if next_q is not None:
            self.active_question = next_q
        elif not self._has_confirmed_once:
            # First time all answered — auto-enter confirmation
            self._phase = _Phase.CONFIRMING
            self._confirm_cursor = 0
            self._render_all()
        else:
            # Returned from confirmation to edit — stay put, require ctrl+enter
            self._render_all()

    def action_prev_question(self) -> None:
        if self._future.done() or self._phase == _Phase.CONFIRMING:
            return
        self.active_question = (self.active_question - 1) % len(self._questions)

    def action_next_question(self) -> None:
        if self._future.done() or self._phase == _Phase.CONFIRMING:
            return
        self.active_question = (self.active_question + 1) % len(self._questions)

    def action_submit(self) -> None:
        if self._future.done():
            return
        if self._phase == _Phase.CONFIRMING:
            return
        if len(self._answers) == len(self._questions):
            result = {
                self._questions[i]["name"]: answer
                for i, answer in self._answers.items()
            }
            self.resolve(result)
            self._show_final_summary()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_next_unanswered(self) -> int | None:
        """Find the next unanswered question after the current one, wrapping."""
        n = len(self._questions)
        for offset in range(1, n + 1):
            idx = (self.active_question + offset) % n
            if idx not in self._answers:
                return idx
        return None

    def _show_final_summary(self) -> None:
        """Transition into collapsible resolved state."""
        self.query_one("#mc-hint", Static).update("")
        self.query_one("#mc-hint", Static).display = False
        self.can_focus = True
        self.query_one("#mc-collapse", Button).display = True
        self._set_collapsed(True)

    # ------------------------------------------------------------------
    # Collapse / expand (post-resolution)
    # ------------------------------------------------------------------

    def _build_summary_text(self) -> Text:
        """Build collapsed summary: 'Q1: A1, Q2: A2, ...'."""
        summary = Text()
        parts: list[str] = []
        for i, q in enumerate(self._questions):
            answer = self._answers.get(i, "—")
            parts.append(f"{q['name']}: {answer}")
        summary.append("  " + ", ".join(parts), style=_DIM)
        return summary

    def _build_expanded_answers(self) -> Text:
        """Build the expanded view showing all questions and their answers."""
        text = Text()
        for i, q in enumerate(self._questions):
            if i > 0:
                text.append("\n")
            answer = self._answers.get(i, "—")
            text.append(f"  {q['name']}: ", style=_DIM)
            text.append(answer, style="rgb(100,200,100)")
        return text

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        btn = self.query_one("#mc-collapse", Button)
        btn.label = "▶" if collapsed else "▼"

        summary_widget = self.query_one("#mc-collapsed-summary", Static)
        tabs = self.query_one("#mc-tabs", Static)
        prompt = self.query_one("#mc-prompt", Static)
        options = self.query_one("#mc-options", Static)

        if collapsed:
            summary_widget.update(self._build_summary_text())
            summary_widget.display = True
            tabs.display = False
            prompt.display = False
            options.display = False
        else:
            summary_widget.display = False
            tabs.display = True
            prompt.display = False  # no need for prompt text in expanded resolved
            options.display = True
            options.update(self._build_expanded_answers())
            # Show tabs in resolved state (all checked, dimmed)
            self._render_resolved_tabs()

    def _render_resolved_tabs(self) -> None:
        """Render tabs in read-only resolved state."""
        text = Text()
        for i, q in enumerate(self._questions):
            if i > 0:
                text.append("  ")
            label = f"[x] {q['name']}"
            text.append(label, style="rgb(100,200,100)")
        self.query_one("#mc-tabs", Static).update(text)

    def cancel(self) -> None:
        super().cancel()
        self.query_one("#mc-tabs", Static).display = False
        self.query_one("#mc-hint", Static).display = False
        self.query_one("#mc-prompt", Static).display = False
        self.query_one("#mc-options", Static).display = False
        summary = self.query_one("#mc-collapsed-summary", Static)
        summary.update(Text("  cancelled", style=_DIM))
        summary.display = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mc-collapse":
            event.stop()
            self._set_collapsed(not self._collapsed)

