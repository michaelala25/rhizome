"""Buffered-edit VM for the entry detail side panel (right of the entry table in
``EntryTab``).

Editing model: each field has an in-VM buffer seeded from the entry on ``set_entry``. When either
buffer diverges from the stored value the VM is ``is_dirty`` and the view reveals an Accept/Cancel
row. Accept persists via ``update_entry`` + commit and mutates the in-memory ``KnowledgeEntry`` in
place so the tab VM's row reference picks up the new values without a refetch; Cancel restores from
the entry.

Cursor-move-while-dirty: silent discard. The tab VM calls ``set_entry`` on every cursor move and
buffers are unconditionally reseeded.

Callback groups:
  * ``dirty`` — standard repaint signal.
  * ``SAVED`` — fires only after a successful Accept. The tab VM subscribes to repaint its table row.

This VM is a leaf (no subscriptions). The tab VM is the only outside writer (``set_entry``,
``set_multi_select``); the view drives the buffer mutators and accept/cancel.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rhizome.db import KnowledgeEntry
from rhizome.db.operations import update_entry
from rhizome.logs import get_logger

from rhizome.app.vm import ViewModelBase

_logger = get_logger("browser.entry_details")


class EntryDetailsVM(ViewModelBase):
    """Buffered-edit VM for the entry detail panel. Accept/Cancel are the explicit exits from dirty;
    nothing reaches the DB until the user Accepts."""

    class Callbacks(Enum):
        SAVED = "saved"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._saved = self._make_group(EntryDetailsVM.Callbacks.SAVED)

        self._entry: KnowledgeEntry | None = None
        # Buffers shadow the entry's stored values. Seeded on every ``set_entry`` so the dirty test
        # is a plain string compare with no extra state.
        self._title_buffer: str = ""
        self._content_buffer: str = ""

        # Freeze flag pushed by the tab VM when multi-select is on. Title/content still track the
        # cursor row; the view switches the ``TextArea``s to read-only and hides Accept/Cancel.
        self._multi_select_active: bool = False
        self._multi_select_count: int = 0

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def saved(self):
        return self._saved

    @property
    def entry(self) -> KnowledgeEntry | None:
        return self._entry

    @property
    def title(self) -> str:
        """The buffer — what the user is currently editing. The view binds the title ``TextArea`` to
        this, not to the entry's stored value."""
        return self._title_buffer

    @property
    def content(self) -> str:
        """Buffer; same as ``title``."""
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

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def multi_select_count(self) -> int:
        return self._multi_select_count

    # ------------------------------------------------------------------
    # Mutators — tab-side
    # ------------------------------------------------------------------

    def set_entry(self, entry: KnowledgeEntry | None) -> None:
        """Switch the panel to ``entry`` and reseed buffers. Identity check so the same entry shown
        twice is a no-op. Silently discards any in-flight edits on the previous entry."""
        if self._entry is entry:
            return
        self._entry = entry
        self._title_buffer = "" if entry is None else entry.title
        self._content_buffer = "" if entry is None else entry.content
        self.emit(self.dirty)

    def set_multi_select(self, active: bool, count: int) -> None:
        """Push from the tab VM on multi-select toggle or selection-size change. Equality-guarded so
        it's safe to call on every selection toggle."""
        if (active, count) == (self._multi_select_active, self._multi_select_count):
            return
        self._multi_select_active = active
        self._multi_select_count = count
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Mutators — view-side (TextArea change handlers)
    # ------------------------------------------------------------------

    def set_title(self, value: str) -> None:
        # Equality early-return absorbs the round-trip from our own ``_refresh`` assignment.
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

    async def accept(self) -> None:
        """Persist the buffers and mutate the in-memory entry in place. Emits ``saved`` so the tab
        VM can repaint its table row; ``saved`` consumers can trust ``entry.title`` / ``entry.content``
        because we mutate after the commit."""
        if self._entry is None or not self.is_dirty:
            return
        async with self._session_factory() as session:
            await update_entry(
                session,
                self._entry.id,
                title=self._title_buffer,
                content=self._content_buffer,
            )
            await session.commit()
        self._entry.title = self._title_buffer
        self._entry.content = self._content_buffer
        self.emit(self.dirty)
        self.emit(self._saved)

    def cancel(self) -> None:
        """Discard the buffers and return to the entry's stored values."""
        if self._entry is None or not self.is_dirty:
            return
        self._title_buffer = self._entry.title
        self._content_buffer = self._entry.content
        self.emit(self.dirty)
