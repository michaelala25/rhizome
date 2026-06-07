"""``FlashcardProposalModel`` — VM for the flashcard-proposal review surface.

State machine
-------------
``REVIEWING`` is the resting state: the user is tweaking flashcards (inline question / answer /
testing-notes edits, exclusions, topic reassignment) and may either ``accept()`` or transition to
``REQUESTING_REVISION`` via ``request_revision()`` to ask the agent to redo the proposal with
natural-language feedback.

``REQUESTING_REVISION`` is the same as ``REVIEWING`` for per-flashcard mutators (inline edits
remain valid — the agent's revision tool consumes both the user's edits and the feedback
together) but gates the terminal action to ``submit_revision(feedback)``. ``cancel_revision()``
returns to ``REVIEWING`` without ending the proposal.

``DONE`` is terminal. ``outcome`` reports what was decided (``ACCEPTED`` / ``REVISED`` /
``CANCELLED``); ``revision_feedback`` carries the feedback text iff ``outcome is REVISED``.

Buffer ownership
----------------
The view holds the editable buffers for question / answer / testing notes and for revision
feedback. The VM receives finalised values at confirm points (``set_flashcard_question`` etc.
after the details-panel Accept gesture, ``submit_revision(text)`` when the revision menu's
Submit fires). The VM never round-trips per-keystroke text changes.

The ``entry_ids`` field on each ``Flashcard`` is read-only from the widget's perspective — the
view displays the list but never mutates it. Relinking is a future task.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any

from rhizome.app.model import ViewModelBase
from rhizome.db import Topic


@dataclass
class Flashcard:
    """A single pending flashcard write in a flashcard proposal."""

    question: str
    answer: str
    testing_notes: str
    topic: Topic | None
    entry_ids: list[int] = field(default_factory=list)

    def clone(self) -> "Flashcard":
        """Field-by-field copy. Used by ``FlashcardProposalModel`` to snapshot the initial proposal
        for ``reset``. Shallow on ``topic`` — Topic instances are treated as immutable references.
        ``entry_ids`` is copied so snapshot and working list don't share mutable state."""
        return replace(self, entry_ids=list(self.entry_ids))

    @classmethod
    def from_dict(cls, d: dict[str, Any], topic: Topic | None = None) -> "Flashcard":
        return cls(
            question=d.get("question", ""),
            answer=d.get("answer", ""),
            testing_notes=d.get("testing_notes") or "",
            topic=topic,
            entry_ids=list(d.get("entry_ids") or []),
        )


class FlashcardProposalModel(ViewModelBase):

    class State(Enum):
        REVIEWING           = auto()
        REQUESTING_REVISION = auto()
        DONE                = auto()

    class Outcome(Enum):
        ACCEPTED  = auto()
        REVISED   = auto()
        CANCELLED = auto()

    class Callbacks(ViewModelBase.Callbacks):
        OnFlashcardsChanged = "OnFlashcardsChanged"
        OnRevisingChanged   = "OnRevisingChanged"
        OnDone              = "OnDone"

    def __init__(self, flashcards: list[Flashcard], *, session_factory: Any = None) -> None:
        super().__init__()

        self.session_factory = session_factory

        self._initial:   list[Flashcard] = [f.clone() for f in flashcards]
        self.flashcards: list[Flashcard] = [f.clone() for f in flashcards]
        self.excluded:   set[int]        = set()

        self._state:             FlashcardProposalModel.State            = FlashcardProposalModel.State.REVIEWING
        self._outcome:           FlashcardProposalModel.Outcome | None   = None
        self._revision_feedback: str | None                              = None

        self.make_callback_groups({
            self.Callbacks.OnFlashcardsChanged: list[int],
            self.Callbacks.OnRevisingChanged:   bool,
            self.Callbacks.OnDone:              FlashcardProposalModel.Outcome,
        })

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> "FlashcardProposalModel.State":
        return self._state

    @property
    def outcome(self) -> "FlashcardProposalModel.Outcome | None":
        return self._outcome

    @property
    def revision_feedback(self) -> str | None:
        return self._revision_feedback

    @property
    def is_done(self) -> bool:
        return self._state == FlashcardProposalModel.State.DONE

    @property
    def is_revising(self) -> bool:
        return self._state == FlashcardProposalModel.State.REQUESTING_REVISION

    @property
    def cancelled(self) -> bool:
        return self._outcome is FlashcardProposalModel.Outcome.CANCELLED

    @property
    def accepted_flashcards(self) -> list[Flashcard]:
        return [deepcopy(f) for i, f in enumerate(self.flashcards) if i not in self.excluded]

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    # ------------------------------------------------------------------
    # Per-flashcard mutators — valid in REVIEWING and REQUESTING_REVISION
    # ------------------------------------------------------------------

    def set_excluded(self, idx: int, excluded: bool) -> None:
        self._assert_open()

        if excluded == (idx in self.excluded):
            return

        if excluded:
            self.excluded.add(idx)
        else:
            self.excluded.discard(idx)

        self.emit(self.Callbacks.OnFlashcardsChanged, [idx])

    def toggle_excluded(self, idx: int) -> bool:
        new = not self.is_excluded(idx)
        self.set_excluded(idx, new)
        return new

    def set_flashcard_topic(self, idx: int, topic: Topic) -> None:
        self._assert_open()
        flashcard = self.flashcards[idx]

        if flashcard.topic is not None and flashcard.topic.id == topic.id:
            return

        flashcard.topic = topic
        self.emit(self.Callbacks.OnFlashcardsChanged, [idx])

    def set_topic_all(self, topic: Topic) -> None:
        self._assert_open()

        dirty: list[int] = []
        for i, f in enumerate(self.flashcards):
            if f.topic is None or f.topic.id != topic.id:
                f.topic = topic
                dirty.append(i)

        if dirty:
            self.emit(self.Callbacks.OnFlashcardsChanged, dirty)

    def set_flashcard_question(self, idx: int, text: str) -> None:
        self._assert_open()
        flashcard = self.flashcards[idx]

        if flashcard.question == text:
            return

        flashcard.question = text
        self.emit(self.Callbacks.OnFlashcardsChanged, [idx])

    def set_flashcard_answer(self, idx: int, text: str) -> None:
        self._assert_open()
        flashcard = self.flashcards[idx]

        if flashcard.answer == text:
            return

        flashcard.answer = text
        self.emit(self.Callbacks.OnFlashcardsChanged, [idx])

    def set_flashcard_testing_notes(self, idx: int, text: str) -> None:
        self._assert_open()
        flashcard = self.flashcards[idx]

        if flashcard.testing_notes == text:
            return

        flashcard.testing_notes = text
        self.emit(self.Callbacks.OnFlashcardsChanged, [idx])

    # ------------------------------------------------------------------
    # Revision lifecycle
    # ------------------------------------------------------------------

    def request_revision(self) -> None:
        assert self._state == FlashcardProposalModel.State.REVIEWING

        self._state = FlashcardProposalModel.State.REQUESTING_REVISION
        self.emit(self.Callbacks.OnRevisingChanged, True)

    def cancel_revision(self) -> None:
        assert self._state == FlashcardProposalModel.State.REQUESTING_REVISION

        self._state = FlashcardProposalModel.State.REVIEWING
        self.emit(self.Callbacks.OnRevisingChanged, False)

    def submit_revision(self, feedback: str) -> None:
        assert self._state == FlashcardProposalModel.State.REQUESTING_REVISION

        self._revision_feedback = feedback
        self._state = FlashcardProposalModel.State.DONE
        self._outcome = FlashcardProposalModel.Outcome.REVISED
        self.emit(self.Callbacks.OnDone, self._outcome)

    # ------------------------------------------------------------------
    # Terminal lifecycle
    # ------------------------------------------------------------------

    def accept(self) -> None:
        assert self._state == FlashcardProposalModel.State.REVIEWING

        self._state = FlashcardProposalModel.State.DONE
        self._outcome = FlashcardProposalModel.Outcome.ACCEPTED
        self.emit(self.Callbacks.OnDone, self._outcome)

    def cancel(self) -> None:
        self._assert_open()

        self._state = FlashcardProposalModel.State.DONE
        self._outcome = FlashcardProposalModel.Outcome.CANCELLED
        self.emit(self.Callbacks.OnDone, self._outcome)

    def reset(self) -> None:
        self._assert_open()

        dirty = {i for i, f in enumerate(self.flashcards) if self._initial[i] != f}
        dirty |= self.excluded

        self.flashcards = [f.clone() for f in self._initial]
        self.excluded.clear()

        if dirty:
            self.emit(self.Callbacks.OnFlashcardsChanged, sorted(dirty))

        if self._state == FlashcardProposalModel.State.REQUESTING_REVISION:
            self._state = FlashcardProposalModel.State.REVIEWING
            self.emit(self.Callbacks.OnRevisingChanged, False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_open(self) -> None:
        assert self._state != FlashcardProposalModel.State.DONE, (
            "Cannot mutate a FlashcardProposalModel after it reaches DONE."
        )
