"""Buffered-edit VM for the focused entry's title/content in a commit proposal.

Mirrors ``rhizome.app.browser.tabs.entries.entry_details.EntryDetailsVM`` in shape — a per-field
buffer seeded from the underlying object on ``set_entry``, an ``is_dirty`` derived from a plain
string compare, and an Accept/Cancel exit pair. The difference vs. the browser variant is the
write-back target: there is no DB here. ``accept()`` mutates the in-memory ``Entry`` dataclass in
place; the parent ``CommitProposalVM`` is responsible for ultimately committing the proposal as a
whole when the user accepts everything.

Cursor-move-while-dirty: silent discard, matching the browser variant. The parent VM calls
``set_entry`` on every cursor move and unconditionally reseeds the buffers.
"""

from __future__ import annotations

from rhizome.app.commit_proposal.entry import Entry
from rhizome.app.vm import ViewModelBase


class EntryDetailsVM(ViewModelBase):
    """Per-entry buffered edit of title/content. Leaf VM — emits only ``dirty``."""

    def __init__(self) -> None:
        super().__init__()
        self._entry: Entry | None = None
        # Buffers shadow the entry's stored fields. Seeded on every ``set_entry`` so the dirty test
        # is a plain string compare with no extra state.
        self._title_buffer: str = ""
        self._content_buffer: str = ""

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def entry(self) -> Entry | None:
        return self._entry

    @property
    def title(self) -> str:
        return self._title_buffer

    @property
    def content(self) -> str:
        return self._content_buffer

    @property
    def original_title(self) -> str:
        return "" if self._entry is None else self._entry.title

    @property
    def original_content(self) -> str:
        return "" if self._entry is None else self._entry.content

    @property
    def is_dirty(self) -> bool:
        """True iff either buffer diverges from the entry. False when no entry is loaded."""
        if self._entry is None:
            return False
        return (
            self._title_buffer != self._entry.title
            or self._content_buffer != self._entry.content
        )

    # ------------------------------------------------------------------
    # Mutators — parent-side
    # ------------------------------------------------------------------

    def set_entry(self, entry: Entry | None) -> None:
        """Switch the panel to ``entry`` and reseed buffers. Identity check so re-pointing at the
        same entry is a no-op. Silently discards any in-flight edits on the previous entry."""
        if self._entry is entry:
            return
        self._entry = entry
        self._title_buffer = "" if entry is None else entry.title
        self._content_buffer = "" if entry is None else entry.content
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Mutators — view-side (TextArea change handlers)
    # ------------------------------------------------------------------

    def set_title(self, value: str) -> None:
        if self._entry is None:
            return
        if value == self._title_buffer:
            return
        self._title_buffer = value
        self.emit(self.dirty)

    def set_content(self, value: str) -> None:
        if self._entry is None:
            return
        if value == self._content_buffer:
            return
        self._content_buffer = value
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Accept / Cancel
    # ------------------------------------------------------------------

    def accept(self) -> None:
        """Write the buffers back into the underlying ``Entry`` dataclass in place. No DB, no
        ``saved`` group — the parent VM will reflect the change via its own ``dirty`` emit when it
        observes ours."""
        if self._entry is None or not self.is_dirty:
            return
        self._entry.title = self._title_buffer
        self._entry.content = self._content_buffer
        self.emit(self.dirty)

    def cancel(self) -> None:
        """Discard the buffers and return to the entry's stored values."""
        if self._entry is None or not self.is_dirty:
            return
        self._title_buffer = self._entry.title
        self._content_buffer = self._entry.content
        self.emit(self.dirty)
