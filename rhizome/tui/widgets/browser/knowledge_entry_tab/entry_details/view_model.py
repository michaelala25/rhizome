"""EntryDetailsViewModel — buffered-edit VM for the title/content side panel that sits to the right of the
entry table in ``KnowledgeEntryBrowserTabView``.

Editing model: **buffered with explicit accept/cancel**. The title input and content textarea write to
in-VM buffers (``_title_buffer``, ``_content_buffer``) that are seeded from the entry on ``set_entry``. As
soon as either buffer diverges from the entry's stored value the VM flips into a dirty state and the view
reveals a two-line choices list ("Accept" / "Cancel") below the content area. The user navigates that with
arrows and confirms with enter, which either calls ``update_entry`` + commits + mutates the in-memory entry
in place (Accept) or resets the buffers (Cancel).

Cursor-move-while-dirty policy: **silent discard**. ``set_entry`` is called by the tab VM on every cursor
move; it reseeds the buffers from the new entry, so any unsaved edits to the previous entry are lost. The
user must explicitly Accept before moving on.

The VM emits two distinct callback groups:

  * ``dirty`` — the usual repaint signal (buffer changed, entry changed, choice cursor moved, accept/cancel
    landed).
  * ``saved`` — fires only on successful Accept. The tab VM subscribes so it can repaint its table row (the
    in-memory ``KnowledgeEntry`` was mutated in place, but the ``DataTable`` doesn't know that yet).

The VM is still a leaf — no subscriptions of its own. The tab VM is the only writer (it calls
``set_entry``); the view drives the buffer mutators and the accept/cancel actions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rhizome.db import KnowledgeEntry
from rhizome.db.operations import update_entry
from rhizome.logs import get_logger

from ....view_model_base import ViewModelBase

_logger = get_logger("browser.entry_details")


class EntryDetailsViewModel(ViewModelBase):
    """Buffered-edit VM for the entry detail panel.

    Holds the current entry plus per-field buffers; flips into a dirty state when either buffer diverges
    from the entry's stored value. Accept/Cancel are the explicit exits from dirty. Until the user Accepts,
    nothing reaches the DB.
    """

    class Callbacks(Enum):
        # Standard dirty + focus inherited. ``SAVED`` is browser-specific: the tab VM subscribes so it can
        # repaint its table row after a successful write.
        SAVED = "saved"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._saved = self._make_group(EntryDetailsViewModel.Callbacks.SAVED)

        self._entry: KnowledgeEntry | None = None
        # Buffers shadow the entry's stored values. Seeded on every ``set_entry`` so the dirty test (buffer
        # != entry.field) is a plain string compare with no extra state.
        self._title_buffer: str = ""
        self._content_buffer: str = ""

        # 0 = Accept, 1 = Cancel. Reset on every entry change and on accept/cancel completion. Modulo-2 wrap
        # on arrow nav.
        self._choice_cursor: int = 0

        # Freeze flag pushed by the tab VM when the user enters multi-select mode. Title/content remain
        # visible (they still track the cursor row) but the view switches the ``TextArea``s to read-only and
        # hides the Accept/Cancel choices. ``_count`` is the size of the tab VM's selection set; the view
        # can surface it once it grows beyond a placeholder.
        self._multi_select_active: bool = False
        self._multi_select_count: int = 0

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def saved(self):
        return self._saved

    @property
    def entry(self) -> KnowledgeEntry | None:
        return self._entry

    @property
    def title(self) -> str:
        """The *buffer* — i.e. what the user is currently editing. The view binds the title ``Input`` to
        this, not to the entry's stored value."""
        return self._title_buffer

    @property
    def content(self) -> str:
        """Buffer; same rationale as ``title``."""
        return self._content_buffer

    @property
    def original_title(self) -> str:
        return "" if self._entry is None else self._entry.title

    @property
    def original_content(self) -> str:
        return "" if self._entry is None else self._entry.content

    @property
    def is_dirty(self) -> bool:
        """True when either buffer diverges from the entry's stored value. Always False when there's no
        entry (nothing to edit)."""
        if self._entry is None:
            return False
        return (
            self._title_buffer != self._entry.title
            or self._content_buffer != self._entry.content
        )

    @property
    def choice_cursor(self) -> int:
        return self._choice_cursor

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def multi_select_count(self) -> int:
        return self._multi_select_count

    # ------------------------------------------------------------------
    # Mutators (display side — called by the tab VM)
    # ------------------------------------------------------------------

    def set_entry(self, entry: KnowledgeEntry | None) -> None:
        """Switch the panel to display ``entry``. Reseeds the buffers from the new entry's stored values,
        silently discarding any in-flight edits to the previous entry (per the cursor-move-while-dirty
        policy). Identity check rather than equality so the same entry re-shown across two ``_sync_details``
        calls is a no-op."""
        if self._entry is entry:
            return
        self._entry = entry
        self._title_buffer = "" if entry is None else entry.title
        self._content_buffer = "" if entry is None else entry.content
        self._choice_cursor = 0
        self.emit(self.dirty)

    def set_multi_select(self, active: bool, count: int) -> None:
        """Push from the tab VM whenever it toggles multi-select mode or the selection set grows/shrinks.
        Equality-guarded so this is safe to call on every selection toggle."""
        if (active, count) == (self._multi_select_active, self._multi_select_count):
            return
        self._multi_select_active = active
        self._multi_select_count = count
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Mutators (edit side — called by the view's change handlers)
    # ------------------------------------------------------------------

    def set_title(self, value: str) -> None:
        """Update the title buffer. No-op when ``value`` already matches — absorbs the round-trip from the
        view's own ``input.value =`` and keeps stale ``Input.Changed`` events (see the view) from emitting
        spurious dirties."""
        if self._entry is None:
            return
        if value == self._title_buffer:
            return
        self._title_buffer = value
        self.emit(self.dirty)

    def set_content(self, value: str) -> None:
        """Update the content buffer. See ``set_title`` for the no-op rationale."""
        if self._entry is None:
            return
        if value == self._content_buffer:
            return
        self._content_buffer = value
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Choice cursor + accept/cancel
    # ------------------------------------------------------------------

    def move_choice_cursor(self, direction: int) -> None:
        """Move the Accept/Cancel cursor. No-op when the choices list isn't meaningful (clean state)."""
        if not self.is_dirty:
            return
        new = (self._choice_cursor + direction) % 2
        if new == self._choice_cursor:
            return
        self._choice_cursor = new
        self.emit(self.dirty)

    async def accept(self) -> None:
        """Persist the current buffers to the DB and mutate the in-memory entry in place.

        Mutating the entry instance after the write means the tab VM's ``self._entries[i]`` reference picks
        up the new values for free — no refetch needed. We then emit ``saved`` so the tab VM can repaint
        its table row with the new title. After this returns ``is_dirty`` is False and the choices list
        disappears naturally on the next refresh.

        No-op when there's nothing dirty to save (defensive — the choice confirm path is guarded by
        visibility, but a stray binding could still fire here)."""
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
        # Bring the in-memory entry in sync with the persisted values *after* the commit so any view
        # subscribers seeing ``saved`` can trust ``entry.title`` / ``entry.content``.
        self._entry.title = self._title_buffer
        self._entry.content = self._content_buffer
        self._choice_cursor = 0
        self.emit(self.dirty)
        self.emit(self._saved)

    def cancel(self) -> None:
        """Discard the buffers and return to the entry's stored values."""
        if self._entry is None or not self.is_dirty:
            return
        self._title_buffer = self._entry.title
        self._content_buffer = self._entry.content
        self._choice_cursor = 0
        self.emit(self.dirty)
