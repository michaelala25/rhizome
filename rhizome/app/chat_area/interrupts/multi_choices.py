"""MultipleChoices interrupt — MVVM port of the legacy ``multiple_choices.py`` widget.

Presents N questions, each with its own option list. The user moves between questions horizontally
(``ctrl+left`` / ``ctrl+right``) and chooses an answer per question vertically (``up`` / ``down`` /
``enter``). Once every question has an answer the VM enters a ``CONFIRMING`` phase showing a
``"Submit answers?"`` Yes/No prompt. Yes resolves the future with a ``dict[name -> answer]``; No
returns to ``ANSWERING`` with a sticky ``_has_confirmed_once`` flag so future "all answered"
transitions don't re-auto-confirm.

The VM owns the future (via ``InterruptModelBase``); the view is a passive projection that just
forwards key actions to VM mutators and re-renders on ``dirty``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from rhizome.app.chat_area.interrupts.base import InterruptModelBase

_DIM = "rgb(100,100,100)"
_ANSWERED = "rgb(100,200,100)"


class MultiUserChoicesModel(InterruptModelBase):
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
        self._phase: MultiUserChoicesModel.Phase = MultiUserChoicesModel.Phase.ANSWERING
        self._confirm_cursor: int = 0
        self._has_confirmed_once: bool = False

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> MultiUserChoicesModel:
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
        if self._phase is MultiUserChoicesModel.Phase.CONFIRMING:
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

        if self._phase is MultiUserChoicesModel.Phase.CONFIRMING:
            new = (self._confirm_cursor + delta) % 2
            if new == self._confirm_cursor:
                return
            self._confirm_cursor = new
            self.emit(self.Callbacks.OnDirty)
            return

        options = self._questions[self._active_question]["options"]
        if not options:
            return
        current = self._per_question_cursor.get(self._active_question, 0)
        new = (current + delta) % len(options)
        if new == current:
            return
        self._per_question_cursor[self._active_question] = new
        self.emit(self.Callbacks.OnDirty)

    def prev_question(self) -> None:
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesModel.Phase.ANSWERING:
            return
        n = len(self._questions)
        if n <= 1:
            return
        self._active_question = (self._active_question - 1) % n
        self.emit(self.Callbacks.OnDirty)

    def next_question(self) -> None:
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesModel.Phase.ANSWERING:
            return
        n = len(self._questions)
        if n <= 1:
            return
        self._active_question = (self._active_question + 1) % n
        self.emit(self.Callbacks.OnDirty)

    def confirm(self) -> None:
        """Phase-dependent confirm:

        - ANSWERING: record answer for the active question. Advance to the next unanswered question if
          any. Otherwise, on the first all-answered event auto-enter CONFIRMING; on subsequent ones
          (user edited after declining) stay put — they must press ``submit()`` (ctrl+enter) explicitly.
        - CONFIRMING: Yes resolves; No returns to ANSWERING and flips ``_has_confirmed_once``.
        """
        if self.resolved:
            return

        if self._phase is MultiUserChoicesModel.Phase.CONFIRMING:
            if self._confirm_cursor == 0:
                self.resolve(self._build_result())
            else:
                self._has_confirmed_once = True
                self._phase = MultiUserChoicesModel.Phase.ANSWERING
                self.emit(self.Callbacks.OnDirty)
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
            self._phase = MultiUserChoicesModel.Phase.CONFIRMING
            self._confirm_cursor = 0
        # else: stay put; user must press submit() to re-resolve.

        self.emit(self.Callbacks.OnDirty)

    def submit(self) -> None:
        """Explicit ctrl+enter submit from ANSWERING. No-op in CONFIRMING (use ``confirm()`` there)."""
        if self.resolved:
            return
        if self._phase is not MultiUserChoicesModel.Phase.ANSWERING:
            return
        if not self.all_answered:
            return
        self.resolve(self._build_result())
