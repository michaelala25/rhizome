"""CommitProposalVM — parent VM for the commit-proposal review surface.

Owns the entry list, the cursor over it, the exclusion set, the edit-instructions buffer, and the
coarse EDITING → DONE lifecycle. Holds one child ``EntryDetailsVM`` that tracks the cursor entry's
buffered title/content; the view binds title/content TextAreas to the child, not to the parent.

State summary
-------------
- ``entries`` — the working list. ``reset()`` restores from the snapshot taken on construction.
- ``cursor`` — index into ``entries``; ``None`` iff ``entries`` is empty.
- ``excluded`` — set of indices the user has marked excluded. Stable across edits (an excluded
  entry stays excluded even if other entries are modified).
- ``edit_instructions`` / ``edit_instructions_visible`` — the natural-language edit-loop input.
  The buffer survives toggling the area's visibility; only ``discard_edit_instructions()`` clears
  the text.
- ``state`` — ``EDITING`` until ``accept_all()`` or ``cancel()`` flips it to ``DONE``.
- ``_cancelled`` — distinguishes the two DONE flavors. The interrupt subclass uses this to pick
  the future's resolution shape.
- ``_collapsed`` — view-side fold flag (auto-set to True on lifecycle exit). Toggleable in DONE
  state only.

Cursor moves push the new entry into ``self.details`` so the title/content TextAreas reseed.
Cursor moves silently discard any in-flight edits — symmetric with the browser's policy and with
``EntryDetailsVM.set_entry``'s reseed-on-identity-change semantics.
"""

from __future__ import annotations

from enum import Enum, auto

from rhizome.app.commit_proposal.entry import Entry, EntryType, cycle_entry_type
from rhizome.app.commit_proposal.entry_details import EntryDetailsVM
from rhizome.app.vm import ViewModelBase


class CommitProposalVM(ViewModelBase):

    class State(Enum):
        EDITING = auto()
        DONE = auto()

    def __init__(self, entries: list[Entry]) -> None:
        super().__init__()

        # Snapshot for ``reset()``. We clone on both the snapshot and the working list so neither
        # aliases the caller's input — mutations on entries are entirely VM-internal.
        self._initial: list[Entry] = [e.clone() for e in entries]
        self.entries: list[Entry] = [e.clone() for e in entries]
        self.excluded: set[int] = set()
        self.cursor: int | None = 0 if self.entries else None

        self.state: CommitProposalVM.State = CommitProposalVM.State.EDITING
        self._cancelled: bool = False
        self._collapsed: bool = False

        self.edit_instructions: str = ""
        self.edit_instructions_visible: bool = False

        # Per-entry buffered edit panel. Seeded with the cursor entry now so the view can bind to
        # populated buffers on first render.
        self._details = EntryDetailsVM()
        self._sync_details()

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

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

    @property
    def details(self) -> EntryDetailsVM:
        return self._details

    @property
    def current_entry(self) -> Entry | None:
        if self.cursor is None:
            return None
        return self.entries[self.cursor]

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    def toggle_collapsed(self) -> None:
        assert self.state == CommitProposalVM.State.DONE
        self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_cursor(self, idx: int | None) -> None:
        # Equality guard absorbs the round-trip from the view's DataTable cursor → RowHighlighted →
        # set_cursor bounce (otherwise we'd loop indefinitely). All cursor movement is view-driven:
        # arrow keys advance the DataTable cursor, the resulting RowHighlighted lands here.
        if not self.entries:
            new_cursor: int | None = None
        elif idx is None:
            new_cursor = None
        else:
            new_cursor = max(0, min(idx, len(self.entries) - 1))
        if new_cursor == self.cursor:
            return
        self.cursor = new_cursor
        self._sync_details()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Per-entry mutators — all operate on the current cursor entry.
    # ------------------------------------------------------------------

    def toggle_exclude_current_entry(self) -> None:
        self._assert_editing()
        if self.cursor is None:
            return
        if self.cursor in self.excluded:
            self.excluded.remove(self.cursor)
        else:
            self.excluded.add(self.cursor)
        self.emit(self.dirty)

    def cycle_current_entry_type(self, *, forward: bool = True) -> None:
        self._assert_editing()
        if self.cursor is None:
            return
        entry = self.entries[self.cursor]
        entry.entry_type = cycle_entry_type(entry.entry_type, forward=forward)
        self.emit(self.dirty)

    def set_current_entry_type(self, entry_type: EntryType) -> None:
        self._assert_editing()
        if self.cursor is None:
            return
        entry = self.entries[self.cursor]
        if entry.entry_type == entry_type:
            return
        entry.entry_type = entry_type
        self.emit(self.dirty)

    def set_current_entry_topic(self, topic_id: int, topic_name: str) -> None:
        """Set the cursor entry's topic. ``topic_id`` + ``topic_name`` are the denormalized pair
        the view obtains from ``TopicSelectorScreen``."""
        self._assert_editing()
        if self.cursor is None:
            return
        entry = self.entries[self.cursor]
        if entry.topic_id == topic_id and entry.topic_name == topic_name:
            return
        entry.topic_id = topic_id
        entry.topic_name = topic_name
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Bulk mutators
    # ------------------------------------------------------------------

    def set_topic_all(self, topic_id: int, topic_name: str) -> None:
        """Reassign every entry to ``topic_id`` / ``topic_name``. No-op if already uniform."""
        self._assert_editing()
        if all(e.topic_id == topic_id and e.topic_name == topic_name for e in self.entries):
            return
        for e in self.entries:
            e.topic_id = topic_id
            e.topic_name = topic_name
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Edit-instructions area
    # ------------------------------------------------------------------

    def toggle_edit_instructions_area(self) -> None:
        """Show/hide the edit-instructions area. The buffer survives — only
        ``discard_edit_instructions`` clears it."""
        self._assert_editing()
        self.edit_instructions_visible = not self.edit_instructions_visible
        self.emit(self.dirty)

    def set_edit_instructions(self, text: str) -> None:
        self._assert_editing()
        if self.edit_instructions == text:
            return
        self.edit_instructions = text
        self.emit(self.dirty)

    def discard_edit_instructions(self) -> None:
        """Clear the buffer. Visibility is left untouched — the area stays open so the user can
        type again immediately if they want."""
        self._assert_editing()
        if not self.edit_instructions:
            return
        self.edit_instructions = ""
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def accept_all(self) -> None:
        """Lock the proposal in. Subclasses (the interrupt VM) observe the state transition and
        resolve their future."""
        self._assert_editing()
        # Drain any in-flight buffer edit on the focused entry first, so an unsaved title/content
        # edit isn't silently discarded by the lifecycle transition. The details VM no-ops if
        # not dirty.
        self._details.accept()
        self.state = CommitProposalVM.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def cancel(self) -> None:
        self._assert_editing()
        self._cancelled = True
        self.state = CommitProposalVM.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def reset(self) -> None:
        """Restore the working list from the initial snapshot. Clears excluded set + edit-
        instructions buffer + hides the instructions area. Cursor is clamped to the restored
        range. No-op semantics are not enforced — this is a user-initiated reset and we want the
        ``dirty`` emit even if nothing visibly changed."""
        self._assert_editing()
        self.entries = [e.clone() for e in self._initial]
        self.excluded.clear()
        if not self.entries:
            self.cursor = None
        elif self.cursor is not None:
            self.cursor = min(self.cursor, len(self.entries) - 1)
        self.edit_instructions = ""
        self.edit_instructions_visible = False
        self._sync_details()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Selection helpers — for downstream consumers of an accepted proposal.
    # ------------------------------------------------------------------

    def accepted_entries(self) -> list[Entry]:
        """The entries the user has *not* excluded, in their original order. Returns clones so
        callers can mutate freely without affecting the VM's state."""
        return [e.clone() for i, e in enumerate(self.entries) if i not in self.excluded]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_editing(self) -> None:
        assert self.state == CommitProposalVM.State.EDITING, (
            f"Mutator called on a CommitProposalVM in state {self.state.name}; mutators are only "
            "valid in EDITING."
        )

    def _sync_details(self) -> None:
        """Push the cursor entry (or ``None``) into the details sub-VM. Called from any cursor-
        moving mutator and from ``reset()``."""
        if self.cursor is None:
            self._details.set_entry(None)
            return
        self._details.set_entry(self.entries[self.cursor])
