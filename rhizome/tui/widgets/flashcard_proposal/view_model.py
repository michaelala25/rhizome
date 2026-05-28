"""FlashcardProposalViewModel — data model + widget state, no view concerns.

Parallel to :class:`CommitProposalViewModel`. The VM owns:
  - the flashcards being proposed,
  - which flashcards the user has chosen to exclude,
  - a cursor over the flashcard list,
  - whether the edit-instructions area is showing, and its buffer,
  - a coarse state (EDITING vs. DONE) and a ``cancelled`` flag, plus a
    ``collapsed`` view-only flag (toggled only in DONE) for the view to fold
    the widget post-resolve.

It does NOT own:
  - which Textual widget is focused,
  - keyboard routing between regions,
  - which choices to render,
  - layout (stacked vs. side-by-side).
"""

from __future__ import annotations

from dataclasses import dataclass, replace, field
from enum import Enum, auto
from typing import Any

from rhizome.app.vm import ViewModelBase


@dataclass
class Flashcard:
    question: str
    answer: str
    testing_notes: str = ""
    entry_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Flashcard":
        return cls(
            question=d.get("question", ""),
            answer=d.get("answer", ""),
            testing_notes=d.get("testing_notes") or "",
            entry_ids=list(d.get("entry_ids") or []),
        )

    def clone(self) -> "Flashcard":
        return replace(self, entry_ids=list(self.entry_ids))


class FlashcardProposalViewModel(ViewModelBase):

    class State(Enum):
        EDITING = auto()
        DONE = auto()

    def __init__(self, flashcards: list[dict[str, Any]]) -> None:
        super().__init__()
        self._initial: list[Flashcard] = [Flashcard.from_dict(f) for f in flashcards]
        self.flashcards: list[Flashcard] = [f.clone() for f in self._initial]
        self.excluded: set[int] = set()
        self.cursor: int | None = 0 if self.flashcards else None
        self.state: FlashcardProposalViewModel.State = (
            FlashcardProposalViewModel.State.EDITING
        )
        self._cancelled: bool = False
        self._collapsed: bool = False
        self.edit_instructions_visible: bool = False
        self.edit_instructions: str = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @collapsed.setter
    def collapsed(self, value: bool) -> None:
        if self._collapsed == value:
            return
        self._collapsed = value
        self.emit(self.dirty)

    def toggle_collapsed(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.DONE
        self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def next_card(self) -> bool:
        if not self.flashcards:
            return False
        if self.cursor is None:
            self.cursor = 0
            self.emit(self.dirty)
            return True
        if self.cursor >= len(self.flashcards) - 1:
            return False
        self.cursor += 1
        self.emit(self.dirty)
        return True

    def prev_card(self) -> bool:
        if not self.flashcards or self.cursor is None:
            return False
        if self.cursor <= 0:
            return False
        self.cursor -= 1
        self.emit(self.dirty)
        return True

    # ------------------------------------------------------------------
    # Field mutators
    # ------------------------------------------------------------------

    def set_card_question(self, idx: int, question: str) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        if self.flashcards[idx].question == question:
            return
        self.flashcards[idx].question = question
        self.emit(self.dirty)

    def set_card_answer(self, idx: int, answer: str) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        if self.flashcards[idx].answer == answer:
            return
        self.flashcards[idx].answer = answer
        self.emit(self.dirty)

    def set_card_testing_notes(self, idx: int, notes: str) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        if self.flashcards[idx].testing_notes == notes:
            return
        self.flashcards[idx].testing_notes = notes
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Exclusion
    # ------------------------------------------------------------------

    def toggle_exclude_current_card(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        if self.cursor is None:
            return
        if self.cursor in self.excluded:
            self.excluded.remove(self.cursor)
        else:
            self.excluded.add(self.cursor)
        self.emit(self.dirty)

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    # ------------------------------------------------------------------
    # Edit-instructions area
    # ------------------------------------------------------------------

    def toggle_edit_instructions_area(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        self.edit_instructions_visible = not self.edit_instructions_visible
        self.emit(self.dirty)

    def set_edit_instructions(self, text: str) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        if self.edit_instructions == text:
            return
        self.edit_instructions = text
        self.emit(self.dirty)

    def discard_edit_instructions(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        self.edit_instructions = ""
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def accept(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        self.state = FlashcardProposalViewModel.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def cancel(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        self._cancelled = True
        self.state = FlashcardProposalViewModel.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def reset(self) -> None:
        assert self.state == FlashcardProposalViewModel.State.EDITING
        self.flashcards = [f.clone() for f in self._initial]
        self.excluded.clear()
        if not self.flashcards:
            self.cursor = None
        elif self.cursor is not None:
            self.cursor = min(self.cursor, len(self.flashcards) - 1)
        self.edit_instructions = ""
        self.edit_instructions_visible = False
        self.emit(self.dirty)
