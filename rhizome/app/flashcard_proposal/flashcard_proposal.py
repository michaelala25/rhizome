"""``FlashcardProposalModel`` — parent VM for the flashcard-proposal review surface.

Owns the flashcard list, the cursor over it, the exclusion set, the edit-instructions buffer, and
the coarse EDITING → DONE lifecycle. Holds one child ``FlashcardDetailsModel`` that tracks the cursor
flashcard's buffered question / answer / testing_notes; the view binds those fields to the child,
not to the parent.

State summary
-------------
- ``flashcards`` — the working list. ``reset()`` restores from the snapshot taken on construction.
- ``cursor`` — index into ``flashcards``; ``None`` iff ``flashcards`` is empty.
- ``excluded`` — set of indices the user has marked excluded. Stable across edits (an excluded
  card stays excluded even if other cards are modified).
- ``edit_instructions`` / ``edit_instructions_visible`` — the natural-language edit-loop input.
  The buffer survives toggling the area's visibility; only ``discard_edit_instructions()`` clears
  the text.
- ``state`` — ``EDITING`` until ``accept_all()`` or ``cancel()`` flips it to ``DONE``. In ``DONE``
  the working state freezes: confirmed edits and the excluded set remain, but every mutator is
  off-limits (each one assertion-guards on ``EDITING``). ``accept_all()`` flushes the in-flight
  details buffer first so an unsaved question/answer/notes edit isn't silently dropped;
  ``cancel()`` does the opposite — discards the buffer via ``self._details.cancel()`` — so a
  half-typed edit on the focused card doesn't survive a rejection.
- ``_cancelled`` — distinguishes the two DONE flavors. The interrupt subclass uses this to pick
  the future's resolution shape. Collapsed/expanded display state for the DONE surface lives on
  the view, not here.

Cursor moves push the new flashcard into ``self.details`` so the question / answer / notes
TextAreas reseed. Cursor moves silently discard any in-flight edits — symmetric with the
browser's policy and with ``FlashcardDetailsModel.set_flashcard``'s reseed-on-identity-change
semantics.
"""

from __future__ import annotations

from enum import Enum, auto

from rhizome.app.flashcard_proposal.flashcard import Flashcard
from rhizome.app.flashcard_proposal.flashcard_details import FlashcardDetailsModel
from rhizome.app.model import ViewModelBase


class FlashcardProposalModel(ViewModelBase):

    class State(Enum):
        EDITING = auto()
        DONE = auto()

    def __init__(self, flashcards: list[Flashcard], *, session_factory=None) -> None:
        super().__init__()

        # Carried for the view's topic-picker modal (the VM doesn't use it itself). ``None`` disables
        # the modal — useful for unit tests and the ``/test-flashcard-proposal`` slash command.
        self.session_factory = session_factory

        # Snapshot for ``reset()``. Clone on both the snapshot and the working list so neither
        # aliases the caller's input — mutations on flashcards are entirely VM-internal.
        self._initial: list[Flashcard] = [f.clone() for f in flashcards]
        self.flashcards: list[Flashcard] = [f.clone() for f in flashcards]
        self.excluded: set[int] = set()
        self.cursor: int | None = 0 if self.flashcards else None

        self.state: FlashcardProposalModel.State = FlashcardProposalModel.State.EDITING
        self._cancelled: bool = False

        self.edit_instructions: str = ""
        self.edit_instructions_visible: bool = False

        # Per-flashcard buffered edit panel. Seeded with the cursor flashcard now so the view can
        # bind to populated buffers on first render.
        self._details = FlashcardDetailsModel()
        self._sync_details()

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def details(self) -> FlashcardDetailsModel:
        return self._details

    @property
    def current_flashcard(self) -> Flashcard | None:
        if self.cursor is None:
            return None
        return self.flashcards[self.cursor]

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_cursor(self, idx: int | None) -> None:
        # Equality guard absorbs the round-trip from the view's DataTable cursor → RowHighlighted →
        # set_cursor bounce (otherwise we'd loop indefinitely). All cursor movement is view-driven:
        # arrow keys advance the DataTable cursor, the resulting RowHighlighted lands here.
        if not self.flashcards:
            new_cursor: int | None = None
        elif idx is None:
            new_cursor = None
        else:
            new_cursor = max(0, min(idx, len(self.flashcards) - 1))
        if new_cursor == self.cursor:
            return
        self.cursor = new_cursor
        self._sync_details()
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Per-flashcard mutators — all operate on the current cursor flashcard.
    # ------------------------------------------------------------------

    def toggle_exclude_current_flashcard(self) -> None:
        self._assert_editing()
        if self.cursor is None:
            return
        if self.cursor in self.excluded:
            self.excluded.remove(self.cursor)
        else:
            self.excluded.add(self.cursor)
        self.emit(self.Callbacks.OnDirty)

    def set_current_flashcard_topic(self, topic_id: int, topic_name: str) -> None:
        """Set the cursor flashcard's topic. ``topic_id`` + ``topic_name`` are the denormalized
        pair the view obtains from ``TopicSelectorScreen``."""
        self._assert_editing()
        if self.cursor is None:
            return
        flashcard = self.flashcards[self.cursor]
        if flashcard.topic_id == topic_id and flashcard.topic_name == topic_name:
            return
        flashcard.topic_id = topic_id
        flashcard.topic_name = topic_name
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Bulk mutators
    # ------------------------------------------------------------------

    def set_topic_all(self, topic_id: int, topic_name: str) -> None:
        """Reassign every flashcard to ``topic_id`` / ``topic_name``. No-op if already uniform."""
        self._assert_editing()
        if all(f.topic_id == topic_id and f.topic_name == topic_name for f in self.flashcards):
            return
        for f in self.flashcards:
            f.topic_id = topic_id
            f.topic_name = topic_name
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Edit-instructions area
    # ------------------------------------------------------------------

    def toggle_edit_instructions_area(self) -> None:
        """Show/hide the edit-instructions area. The buffer survives — only
        ``discard_edit_instructions`` clears it."""
        self._assert_editing()
        self.edit_instructions_visible = not self.edit_instructions_visible
        self.emit(self.Callbacks.OnDirty)

    def set_edit_instructions(self, text: str) -> None:
        self._assert_editing()
        if self.edit_instructions == text:
            return
        self.edit_instructions = text
        self.emit(self.Callbacks.OnDirty)

    def discard_edit_instructions(self) -> None:
        """Clear the buffer. Visibility is left untouched — the area stays open so the user can
        type again immediately if they want."""
        self._assert_editing()
        if not self.edit_instructions:
            return
        self.edit_instructions = ""
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def accept_all(self) -> None:
        """Lock the proposal in. Subclasses (the interrupt VM) observe the state transition and
        resolve their future."""
        self._assert_editing()
        # Drain any in-flight buffer edit on the focused flashcard first, so an unsaved
        # question/answer/notes edit isn't silently discarded by the lifecycle transition. The
        # details VM no-ops if not dirty.
        self._details.accept()
        self.state = FlashcardProposalModel.State.DONE
        self.emit(self.Callbacks.OnDirty)

    def cancel(self) -> None:
        self._assert_editing()
        # Symmetric counterpart to accept_all's flush: drop the in-flight details buffer rather
        # than commit it, so a half-typed edit on the focused flashcard doesn't survive a
        # rejection. No-op if not dirty.
        self._details.cancel()
        self._cancelled = True
        self.state = FlashcardProposalModel.State.DONE
        self.emit(self.Callbacks.OnDirty)

    def reset(self) -> None:
        """Restore the working list from the initial snapshot. Clears excluded set + edit-
        instructions buffer + hides the instructions area. Cursor is clamped to the restored
        range. No-op semantics are not enforced — this is a user-initiated reset and we want the
        ``dirty`` emit even if nothing visibly changed."""
        self._assert_editing()
        self.flashcards = [f.clone() for f in self._initial]
        self.excluded.clear()
        if not self.flashcards:
            self.cursor = None
        elif self.cursor is not None:
            self.cursor = min(self.cursor, len(self.flashcards) - 1)
        self.edit_instructions = ""
        self.edit_instructions_visible = False
        self._sync_details()
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Selection helpers — for downstream consumers of an accepted proposal.
    # ------------------------------------------------------------------

    def accepted_flashcards(self) -> list[Flashcard]:
        """The flashcards the user has *not* excluded, in their original order. Returns clones so
        callers can mutate freely without affecting the VM's state."""
        return [f.clone() for i, f in enumerate(self.flashcards) if i not in self.excluded]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_editing(self) -> None:
        assert self.state == FlashcardProposalModel.State.EDITING, (
            f"Mutator called on a FlashcardProposalModel in state {self.state.name}; mutators are "
            "only valid in EDITING."
        )

    def _sync_details(self) -> None:
        """Push the cursor flashcard (or ``None``) into the details sub-VM. Called from any
        cursor-moving mutator and from ``reset()``."""
        if self.cursor is None:
            self._details.set_flashcard(None)
            return
        self._details.set_flashcard(self.flashcards[self.cursor])
