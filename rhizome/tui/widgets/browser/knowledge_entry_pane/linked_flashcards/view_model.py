"""LinkedFlashcardsPaneViewModel — sub-VM for the right-hand flashcard table shown when the parent
pane is in ``State.LINKED_FLASHCARDS``.

The pane is **cursor-driven**: the parent pushes the highlighted entry's id via ``set_entry_id``
whenever the entries-table cursor moves; this VM owns the per-entry flashcard window, its total /
has-more bookkeeping, its own search query, and an internal row cursor. The view is read-only for
this iteration (just up/down navigation), so there's no multi-select, selection set, or detail
sub-VM here.

Lifecycle
---------
The parent only feeds entry ids while it's in ``State.LINKED_FLASHCARDS`` — when transitioning
*away* it pushes ``set_entry_id(None)`` to clear the window and cancel any in-flight fetch. On
transitioning *back* it pushes the current cursor entry's id. The VM itself doesn't read the parent
state; it just reacts to whatever id (or None) it's given.

Fetch / cancellation
--------------------
Same task-identity guard as ``BrowserPaneViewModel._run_fetch``: each spawned task stamps itself
into ``_current_task`` and only that task is allowed to flip ``is_loading`` back off. A superseded
task's ``finally`` quietly bows out. ``set_entry_id`` and ``set_search`` both go through
``_request_fetch`` which cancels any in-flight task before spawning the next.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rhizome.db import Flashcard
from rhizome.db.operations import (
    count_flashcards_for_entry,
    list_flashcards_for_entry,
)
from rhizome.logs import get_logger

from ....view_model_base import ViewModelBase

_logger = get_logger("browser.linked_flashcards")

# Mirrors ``KnowledgeEntryBrowserPaneViewModel.DEFAULT_PAGE_LIMIT`` so the two tables share a memory
# / render footprint cap. Per-entry flashcard counts are usually small (single digits), so this is
# almost never the binding constraint — but keeping it symmetric means ``load_more`` works the same
# way in both panes if we ever need it.
DEFAULT_PAGE_LIMIT = 500


class LinkedFlashcardsPaneViewModel(ViewModelBase):
    """Sub-VM driving the linked-flashcards table.

    Owns: the current target entry id, the loaded flashcard window, total + has-more, the search
    query, the row cursor, and the loading / cancellation bookkeeping. All mutators are sync — they
    either spawn a background fetch (``_request_fetch``) or update local state and emit ``dirty``.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._limit = limit

        # ``None`` means "no target entry" — used both at boot and when the parent transitions away
        # from ``LINKED_FLASHCARDS``. In that state ``_flashcards`` is forced empty and no fetch
        # runs. This is the only "no data" sentinel the VM has; an entry id that resolves to zero
        # flashcards is a separate, legal state (loaded + empty).
        self._entry_id: int | None = None

        # Loaded window. ``_total`` is ``None`` until the first count query lands (mirrors the
        # entries pane's two-stage fetch). ``_has_more`` is true when the loaded window doesn't
        # cover the full result set.
        self._flashcards: list[Flashcard] = []
        self._total: int | None = None
        self._has_more: bool = False

        # Search query. Empty string = no filter (the DB op treats falsy as None). Survives entry-id
        # changes — the user's filter is a per-pane preference, not per-entry — but resets the
        # window / cursor on apply, same as the entries pane.
        self._search: str = ""

        # Row cursor within the loaded window. Window-local index (not flashcard id), so it points
        # at the same row before and after a ``load_more``. Reset on entry-id change, search
        # change, or when the window shrinks below it.
        self._cursor: int = 0

        # Loading + task-identity guard. Same pattern as ``BrowserPaneViewModel``: only the *current*
        # task is allowed to flip ``_is_loading`` off, so a superseded task's ``finally`` quietly
        # bows out.
        self._is_loading: bool = False
        self._current_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def entry_id(self) -> int | None:
        return self._entry_id

    @property
    def flashcards(self) -> list[Flashcard]:
        return self._flashcards

    @property
    def total(self) -> int | None:
        return self._total

    @property
    def has_more(self) -> bool:
        return self._has_more

    @property
    def search(self) -> str:
        return self._search

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    @property
    def cursor_flashcard(self) -> Flashcard | None:
        """The flashcard currently under the cursor, or ``None`` when the window is empty / cursor is
        out of bounds. Convenience for preview panes that need to read the current row without
        recomputing the bounds-check."""
        if not self._flashcards or self._cursor >= len(self._flashcards):
            return None
        return self._flashcards[self._cursor]

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_entry_id(self, entry_id: int | None) -> None:
        """Set the target entry whose flashcards to show. ``None`` clears the pane and cancels any
        in-flight fetch — used by the parent when transitioning out of ``LINKED_FLASHCARDS``.

        Idempotent: a call with the same id we already hold is a no-op (no cancel, no refetch). The
        cursor and window reset on a real change; search persists across entry-id changes since
        it's a per-pane preference.
        """
        if entry_id == self._entry_id:
            return
        self._entry_id = entry_id
        self._cursor = 0
        if entry_id is None:
            # No target — wipe the window inline. Cancel any in-flight so a late callback doesn't
            # paint stale rows.
            if self._current_task is not None and not self._current_task.done():
                self._current_task.cancel()
            self._flashcards = []
            self._total = 0
            self._has_more = False
            self._is_loading = False
            self.emit(self.dirty)
            return
        self._request_fetch()

    def set_search(self, query: str) -> None:
        """Replace the active question/answer search. Empty string clears the search. Resets the
        window + cursor; refetches against the current entry id (if any)."""
        new = query or ""
        if new == self._search:
            return
        self._search = new
        self._cursor = 0
        if self._entry_id is None:
            # Nothing to fetch yet — just stash the query so it'll apply once an entry lands.
            self.emit(self.dirty)
            return
        self._request_fetch()

    def set_cursor(self, index: int) -> None:
        """Move the row cursor. Clamped to the loaded window. Emits ``dirty`` so the table repaints
        and the answer preview re-reads ``cursor_flashcard``.

        The repaint includes a programmatic ``move_cursor`` on the rebuild path, which fires another
        ``DataTable.RowHighlighted`` and re-enters this method via ``on_data_table_row_highlighted``.
        That second call is a no-op thanks to the index-equality guard below — the bounce dies in one
        round-trip rather than looping.
        """
        if not self._flashcards:
            new = 0
        else:
            new = max(0, min(index, len(self._flashcards) - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of flashcards to the current window. No-op if a fetch is in flight,
        no entry is set, or nothing further is available. Doesn't move the cursor.

        Mirrors ``KnowledgeEntryBrowserPaneViewModel.load_more`` — bypasses ``_request_fetch`` so a
        reset operation that lands mid-flight wins by overwriting ``_flashcards`` after this call
        appends its tail (a mild wasted query for the MVP; lift later if it actually matters).
        """
        if self._is_loading or not self._has_more or self._entry_id is None:
            return
        async with self._session_factory() as session:
            more = await list_flashcards_for_entry(
                session,
                self._entry_id,
                search=self._search or None,
                limit=self._limit,
                offset=len(self._flashcards),
            )
        self._flashcards.extend(more)
        if len(more) < self._limit:
            self._has_more = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Fetch machinery (mirrors BrowserPaneViewModel)
    # ------------------------------------------------------------------

    def _request_fetch(self) -> None:
        """Cancel any in-flight fetch and spawn a fresh one. The cancelled task's ``finally`` block
        sees it's no longer the current task and bows out without touching state."""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
        self._is_loading = True
        self.emit(self.dirty)
        self._current_task = asyncio.create_task(self._run_fetch())

    async def _run_fetch(self) -> None:
        """Wrap ``_fetch`` with task-identity guards and ``is_loading`` bookkeeping. Only the most
        recently-spawned task flips ``_is_loading`` back off; superseded tasks no-op."""
        my_task = asyncio.current_task()
        try:
            await self._fetch()
        except asyncio.CancelledError:
            # Superseded by a later set_entry_id / set_search; ``_entry_id`` already reflects the
            # successor's target, and a fresh task is in flight to fill the new window.
            raise
        except Exception:
            _logger.exception(
                "LinkedFlashcardsPaneViewModel._fetch raised; pane will remain in error "
                "state until next entry-id / search change",
            )
        finally:
            if my_task is self._current_task:
                self._is_loading = False
                self.emit(self.dirty)

    async def _fetch(self) -> None:
        """Reload the window + total against the current entry id and search.

        Two-stage like the entries pane: windowed SELECT first so rows can paint as soon as
        possible, then a separate COUNT for the "showing N of M" hint with ``_has_more`` reconciled
        against the authoritative count. ``_entry_id`` is captured by the surrounding ``set_*``
        mutator before this coroutine ever starts; cancellation drops us out before we touch state.
        """
        assert self._entry_id is not None  # set_entry_id(None) takes the wipe-and-return path
        eid = self._entry_id

        self._total = None
        self._has_more = False

        async with self._session_factory() as session:
            self._flashcards = await list_flashcards_for_entry(
                session,
                eid,
                search=self._search or None,
                limit=self._limit,
                offset=0,
            )
        self._has_more = len(self._flashcards) >= self._limit
        if self._cursor >= len(self._flashcards):
            self._cursor = max(0, len(self._flashcards) - 1)
        self.emit(self.dirty)

        async with self._session_factory() as session:
            self._total = await count_flashcards_for_entry(
                session,
                eid,
                search=self._search or None,
            )
        self._has_more = len(self._flashcards) < self._total
        # Don't emit dirty here — ``_run_fetch``'s finally emits one more after _fetch returns,
        # which covers this last reconciliation.
