"""Buffered-edit VM for the focused flashcard's question / answer / testing-notes fields.

Mirrors ``rhizome.app.commit_proposal.entry_details.EntryDetailsModel`` in shape. Per-field buffer
seeded from the underlying ``Flashcard`` on ``set_flashcard``; ``is_dirty`` is a plain three-way
string compare across the editable fields.

``entry_ids`` is exposed read-only via ``original_entry_ids`` for the view — it has no buffer and
does not participate in ``is_dirty``.

Cursor-move-while-dirty: silent discard, matching the commit-proposal variant. The parent VM
calls ``set_flashcard`` on every cursor move and unconditionally reseeds the buffers.
"""

from __future__ import annotations

from rhizome.app.flashcard_proposal.flashcard import Flashcard
from rhizome.app.model import ViewModelBase


class FlashcardDetailsModel(ViewModelBase):
    """Per-flashcard buffered edit of question / answer / testing_notes. Leaf VM — emits only
    ``dirty``."""

    def __init__(self) -> None:
        super().__init__()
        self._flashcard: Flashcard | None = None
        # Buffers shadow the stored fields. Seeded on every ``set_flashcard`` so the dirty test is
        # a plain string compare with no extra state.
        self._question_buffer: str = ""
        self._answer_buffer: str = ""
        self._testing_notes_buffer: str = ""

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def flashcard(self) -> Flashcard | None:
        return self._flashcard

    @property
    def question(self) -> str:
        return self._question_buffer

    @property
    def answer(self) -> str:
        return self._answer_buffer

    @property
    def testing_notes(self) -> str:
        return self._testing_notes_buffer

    @property
    def original_question(self) -> str:
        return "" if self._flashcard is None else self._flashcard.question

    @property
    def original_answer(self) -> str:
        return "" if self._flashcard is None else self._flashcard.answer

    @property
    def original_testing_notes(self) -> str:
        return "" if self._flashcard is None else self._flashcard.testing_notes

    @property
    def original_entry_ids(self) -> list[int]:
        """Read-only mirror of the underlying flashcard's ``entry_ids``. Returned as a fresh list
        so callers can't mutate the stored list by accident."""
        return [] if self._flashcard is None else list(self._flashcard.entry_ids)

    @property
    def is_dirty(self) -> bool:
        """True iff any of the three editable buffers diverges from the flashcard. False when no
        flashcard is loaded. ``entry_ids`` is not part of the dirty surface — it is non-editable
        from the widget."""
        if self._flashcard is None:
            return False
        return (
            self._question_buffer != self._flashcard.question
            or self._answer_buffer != self._flashcard.answer
            or self._testing_notes_buffer != self._flashcard.testing_notes
        )

    # ------------------------------------------------------------------
    # Mutators — parent-side
    # ------------------------------------------------------------------

    def set_flashcard(self, flashcard: Flashcard | None) -> None:
        """Switch the panel to ``flashcard`` and reseed buffers. Identity check so re-pointing at
        the same flashcard is a no-op. Silently discards any in-flight edits on the previous
        flashcard."""
        if self._flashcard is flashcard:
            return
        self._flashcard = flashcard
        self._question_buffer = "" if flashcard is None else flashcard.question
        self._answer_buffer = "" if flashcard is None else flashcard.answer
        self._testing_notes_buffer = "" if flashcard is None else flashcard.testing_notes
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Mutators — view-side (TextArea change handlers)
    # ------------------------------------------------------------------

    def set_question(self, value: str) -> None:
        if self._flashcard is None:
            return
        if value == self._question_buffer:
            return
        self._question_buffer = value
        self.emit(self.dirty)

    def set_answer(self, value: str) -> None:
        if self._flashcard is None:
            return
        if value == self._answer_buffer:
            return
        self._answer_buffer = value
        self.emit(self.dirty)

    def set_testing_notes(self, value: str) -> None:
        if self._flashcard is None:
            return
        if value == self._testing_notes_buffer:
            return
        self._testing_notes_buffer = value
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Accept / Cancel
    # ------------------------------------------------------------------

    def accept(self) -> None:
        """Write the buffers back into the underlying ``Flashcard`` dataclass in place. No DB —
        the parent ``FlashcardProposalModel`` is responsible for ultimately committing the proposal
        as a whole when the user accepts everything."""
        if self._flashcard is None or not self.is_dirty:
            return
        self._flashcard.question = self._question_buffer
        self._flashcard.answer = self._answer_buffer
        self._flashcard.testing_notes = self._testing_notes_buffer
        self.emit(self.dirty)

    def cancel(self) -> None:
        """Discard the buffers and return to the flashcard's stored values."""
        if self._flashcard is None or not self.is_dirty:
            return
        self._question_buffer = self._flashcard.question
        self._answer_buffer = self._flashcard.answer
        self._testing_notes_buffer = self._flashcard.testing_notes
        self.emit(self.dirty)
