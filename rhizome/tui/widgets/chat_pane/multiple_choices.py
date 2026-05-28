"""MultipleChoices interrupt — MVVM port of the legacy ``multiple_choices.py`` widget.

Presents N questions, each with its own option list. The user moves between questions horizontally
(``ctrl+left`` / ``ctrl+right``) and chooses an answer per question vertically (``up`` / ``down`` /
``enter``). Once every question has an answer the VM enters a ``CONFIRMING`` phase showing a
``"Submit answers?"`` Yes/No prompt. Yes resolves the future with a ``dict[name -> answer]``; No
returns to ``ANSWERING`` with a sticky ``_has_confirmed_once`` flag so future "all answered"
transitions don't re-auto-confirm.

The VM owns the future (via ``InterruptVMBase``); the view is a passive projection that just
forwards key actions to VM mutators and re-renders on ``dirty``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from ..view_base import ViewBase
from .interrupt import InterruptVMBase

_DIM = "rgb(100,100,100)"
_ANSWERED = "rgb(100,200,100)"


class MultiUserChoicesVM(InterruptVMBase):
    """Multi-question single-select interrupt VM. See module docstring."""

    class Phase(Enum):
        ANSWERING = auto()
        CONFIRMING = auto()

    def __init__(self, questions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.is_navigable = True
        # Each question: {"name": str, "prompt": str, "options": list[str]}.
        self._questions: list[dict[str, Any]] = questions
        self._answers: dict[int, str] = {}
        self._per_question_cursor: dict[int, int] = {}
        self._active_question: int = 0
        self._phase: MultiUserChoicesVM.Phase = MultiUserChoicesVM.Phase.ANSWERING
        self._confirm_cursor: int = 0
        self._has_confirmed_once: bool = False

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> MultiUserChoicesVM:
        return cls(questions=value["questions"])

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def questions(self) -> list[dict[str, Any]]:
        return self._questions

    @property
    def answers(self) -> dict[int, str]:
        return dict(self._answers)

    @property
    def active_question(self) -> int:
        return self._active_question

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def confirm_cursor(self) -> int:
        return self._confirm_cursor

    @property
    def cursor(self) -> int:
        """The cursor active on the current axis: per-question answer cursor while ANSWERING, or the
        Yes/No cursor while CONFIRMING.
        """
        if self._phase is MultiUserChoicesVM.Phase.CONFIRMING:
            return self._confirm_cursor
        return self._per_question_cursor.get(self._active_question, 0)

    @property
    def all_answered(self) -> bool:
        return len(self._answers) == len(self._questions)

    def _build_result(self) -> dict[str, str]:
        return {
            self._questions[i]["name"]: answer
            for i, answer in self._answers.items()
        }

    def _find_next_unanswered(self) -> int | None:
        n = len(self._questions)
        for offset in range(1, n + 1):
            idx = (self._active_question + offset) % n
            if idx not in self._answers:
                return idx
        return None

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def move_cursor(self, delta: int) -> None:
        if self.resolved:
            return

        if self._phase is MultiUserChoicesVM.Phase.CONFIRMING:
            new = (self._confirm_cursor + delta) % 2
            if new == self._confirm_cursor:
                return
            self._confirm_cursor = new
            self.emit(self.dirty)
            return

        options = self._questions[self._active_question]["options"]
        if not options:
            return
        current = self._per_question_cursor.get(self._active_question, 0)
        new = (current + delta) % len(options)
        if new == current:
            return
        self._per_question_cursor[self._active_question] = new
        self.emit(self.dirty)

    def prev_question(self) -> None:
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesVM.Phase.ANSWERING:
            return
        n = len(self._questions)
        if n <= 1:
            return
        self._active_question = (self._active_question - 1) % n
        self.emit(self.dirty)

    def next_question(self) -> None:
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesVM.Phase.ANSWERING:
            return
        n = len(self._questions)
        if n <= 1:
            return
        self._active_question = (self._active_question + 1) % n
        self.emit(self.dirty)

    def confirm(self) -> None:
        """Phase-dependent confirm:

        - ANSWERING: record answer for the active question. Advance to the next unanswered question if
          any. Otherwise, on the first all-answered event auto-enter CONFIRMING; on subsequent ones
          (user edited after declining) stay put — they must press ``submit()`` (ctrl+enter) explicitly.
        - CONFIRMING: Yes resolves; No returns to ANSWERING and flips ``_has_confirmed_once``.
        """
        if self.resolved:
            return

        if self._phase is MultiUserChoicesVM.Phase.CONFIRMING:
            if self._confirm_cursor == 0:
                self.resolve(self._build_result())
            else:
                self._has_confirmed_once = True
                self._phase = MultiUserChoicesVM.Phase.ANSWERING
                self.emit(self.dirty)
            return

        options = self._questions[self._active_question]["options"]
        if not options:
            return
        cursor = self._per_question_cursor.get(self._active_question, 0)
        self._answers[self._active_question] = options[cursor]

        next_q = self._find_next_unanswered()
        if next_q is not None:
            self._active_question = next_q
        elif not self._has_confirmed_once:
            self._phase = MultiUserChoicesVM.Phase.CONFIRMING
            self._confirm_cursor = 0
        # else: stay put; user must press submit() to re-resolve.

        self.emit(self.dirty)

    def submit(self) -> None:
        """Explicit ctrl+enter submit from ANSWERING. No-op in CONFIRMING (use ``confirm()`` there)."""
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesVM.Phase.ANSWERING:
            return
        if not self.all_answered:
            return
        self.resolve(self._build_result())


class MultiUserChoices(ViewBase[MultiUserChoicesVM]):
    """Three-region projection of ``MultiUserChoicesVM``: tab bar, prompt, options block, hint.

    After resolution the widget collapses to a single comma-separated summary line (no expand toggle —
    the legacy collapse button was intentionally dropped).
    """

    DEFAULT_CSS = """
    MultiUserChoices {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 0 2;
        border: round rgb(80,80,80);
    }
    MultiUserChoices:focus {
        border: round rgb(140,140,200);
    }
    MultiUserChoices.--resolved {
        border: round rgb(50,50,50);
        color: $text-muted;
    }
    MultiUserChoices #mc-tabs,
    MultiUserChoices #mc-prompt,
    MultiUserChoices #mc-options,
    MultiUserChoices #mc-hint,
    MultiUserChoices #mc-summary {
        height: auto;
        width: 1fr;
    }
    MultiUserChoices #mc-hint {
        color: $text-muted;
    }
    MultiUserChoices #mc-summary {
        display: none;
    }
    MultiUserChoices.--resolved #mc-tabs,
    MultiUserChoices.--resolved #mc-prompt,
    MultiUserChoices.--resolved #mc-options,
    MultiUserChoices.--resolved #mc-hint {
        display: none;
    }
    MultiUserChoices.--resolved #mc-summary {
        display: block;
    }
    """

    BINDINGS = [
        Binding("up", "move_cursor(-1)", "Up", show=False),
        Binding("down", "move_cursor(1)", "Down", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("ctrl+left", "prev_question", "Previous question", show=False),
        Binding("ctrl+right", "next_question", "Next question", show=False),
        # Legacy bound ctrl+enter to ctrl+j (Textual emits ctrl+j for ctrl+enter in many terminals).
        Binding("ctrl+j", "submit", "Submit answers", show=False, priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static(id="mc-tabs")
        yield Static(id="mc-prompt")
        yield Static(id="mc-options")
        yield Static(id="mc-hint")
        yield Static(id="mc-summary")

    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self._vm.resolved:
            self.add_class("--resolved")
            self._refresh_summary()
            return

        self._refresh_tabs()
        self._refresh_prompt()
        self._refresh_options()
        self._refresh_hint()

    def _refresh_tabs(self) -> None:
        Phase = MultiUserChoicesVM.Phase
        text = Text()
        for i, q in enumerate(self._vm.questions):
            if i > 0:
                text.append("  ")
            checked = "x" if i in self._vm.answers else " "
            label = f"[{checked}] {q['name']}"
            if self._vm.phase is Phase.ANSWERING and i == self._vm.active_question:
                text.append(label, style="bold white")
            elif i in self._vm.answers:
                text.append(label, style=_ANSWERED)
            else:
                text.append(label, style=_DIM)
        self.query_one("#mc-tabs", Static).update(text)

    def _refresh_prompt(self) -> None:
        Phase = MultiUserChoicesVM.Phase
        if self._vm.phase is Phase.CONFIRMING:
            self.query_one("#mc-prompt", Static).update("Submit answers?")
        else:
            q = self._vm.questions[self._vm.active_question]
            self.query_one("#mc-prompt", Static).update(q["prompt"])

    def _refresh_options(self) -> None:
        Phase = MultiUserChoicesVM.Phase

        if self._vm.phase is Phase.CONFIRMING:
            options = ["Yes", "No"]
            cursor = self._vm.confirm_cursor
            answered_option: str | None = None
        else:
            options = self._vm.questions[self._vm.active_question]["options"]
            cursor = self._vm.cursor
            answered_option = self._vm.answers.get(self._vm.active_question)

        text = Text()
        for i, option in enumerate(options):
            if i > 0:
                text.append("\n")
            label = f"  {i + 1}. {option}"
            if i == cursor:
                text.append(label, style="bold white")
            elif option == answered_option:
                text.append(label, style=_ANSWERED)
            else:
                text.append(label, style=_DIM)
        self.query_one("#mc-options", Static).update(text)

    def _refresh_hint(self) -> None:
        Phase = MultiUserChoicesVM.Phase
        if self._vm.phase is Phase.CONFIRMING:
            hint = "(enter to confirm, ctrl+c to cancel)"
        elif self._vm.all_answered:
            hint = "(ctrl+left/right to navigate, ctrl+enter to submit, ctrl+c to cancel)"
        else:
            hint = "(ctrl+left/right to navigate between questions, ctrl+c to cancel)"
        self.query_one("#mc-hint", Static).update(hint)

    def _refresh_summary(self) -> None:
        summary = self.query_one("#mc-summary", Static)
        if self._vm.cancelled:
            summary.update(Text("cancelled", style=_DIM))
            return
        answers = self._vm.answers
        parts = [
            f"{q['name']}: {answers.get(i, '—')}"
            for i, q in enumerate(self._vm.questions)
        ]
        summary.update(Text(", ".join(parts), style=_DIM))

    # ------------------------------------------------------------------
    # Actions — all pure VM forwards
    # ------------------------------------------------------------------

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()

    def action_prev_question(self) -> None:
        self._vm.prev_question()

    def action_next_question(self) -> None:
        self._vm.next_question()

    def action_submit(self) -> None:
        self._vm.submit()

    def action_cancel(self) -> None:
        self._vm.cancel()
