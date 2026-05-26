"""LinkedFlashcardsPanelViewModel — sub-VM for the right-hand flashcard table shown when the parent
tab is in ``State.LINKED_FLASHCARDS``.

The tab is **cursor-driven**: the parent pushes the highlighted entry's id via ``set_entry_id``
whenever the entries-table cursor moves; this VM owns the per-entry flashcard window, its total /
has-more bookkeeping, its own search query, and an internal row cursor. The view is read-only for
this iteration (just up/down navigation), so there's no multi-select, selection set, or detail
sub-VM here.

Lifecycle
---------
The parent only feeds entry ids while it's in ``State.LINKED_FLASHCARDS`` — when transitioning
*away* it pushes ``set_entry_id(None)`` to clear the window and invalidate any in-flight fetch.
On transitioning *back* it pushes the current cursor entry's id. The VM itself doesn't read the
parent state; it just reacts to whatever id (or None) it's given.

Fetch strategy
--------------
Mirrors ``BrowserTabViewModel``'s split — stateless ``_fetch`` returns ``(rows, total)``;
``_process_fetched_data`` applies them — but skips the debounce. Per-entry flashcard list + count
queries are sub-millisecond, and ``set_entry_id`` fires on every parent cursor move (dozens of
calls per second under fast scrolling); a 50ms debounce would be perceptible. Instead, in-flight
tasks aren't cancelled — cancelling a SQLAlchemy async session mid-query trips a fragile cleanup
path in the aiosqlite dialect that surfaces ``CancelledError`` out of the pool's
``_finalize_fairy`` and trashes the TUI. Each spawned task captures ``self._fetch_id`` at start
and the result is discarded if the id no longer matches. Stale tasks complete their queries and
exit silently. The wasted DB work is real but cheap (sub-ms × scroll rate) and well below the
cost of contorting around SQLAlchemy's cancellation semantics.
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

# Mirrors ``KnowledgeEntryBrowserTabViewModel.DEFAULT_PAGE_LIMIT`` so the two tables share a memory
# / render footprint cap. Per-entry flashcard counts are usually small (single digits), so this is
# almost never the binding constraint — but keeping it symmetric means ``load_more`` works the same
# way in both tabs if we ever need it.
DEFAULT_PAGE_LIMIT = 500


class LinkedFlashcardsPanelViewModel(ViewModelBase):
    """Sub-VM driving the linked-flashcards table.

    Owns: the current target entry id, the loaded flashcard window, total + has-more, the search
    query, the row cursor, and the loading / staleness bookkeeping. All mutators are sync — they
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
        # runs. An entry id that resolves to zero flashcards is a separate, legal state (loaded +
        # empty).
        self._entry_id: int | None = None

        # Loaded window. ``_total`` is ``None`` until the first count query lands. ``_has_more`` is
        # true when the loaded window doesn't cover the full result set.
        self._flashcards: list[Flashcard] = []
        self._total: int | None = None
        self._has_more: bool = False

        # Search query. Empty string = no filter (the DB op treats falsy as None). Survives entry-id
        # changes — the user's filter is a per-tab preference, not per-entry — but resets the
        # window / cursor on apply, same as the entries tab.
        self._search: str = ""

        # Row cursor within the loaded window. Window-local index (not flashcard id), so it points
        # at the same row before and after a ``load_more``. Reset on entry-id change, search
        # change, or when the window shrinks below it.
        self._cursor: int = 0

        # Loading + fetch-identity guard. Each spawned task captures ``self._fetch_id`` at start;
        # only the most-recent task's result is applied. No cancellation — see the module docstring.
        self._is_loading: bool = False
        self._fetch_id: int = 0

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
        out of bounds. Convenience for preview tabs that need to read the current row without
        recomputing the bounds-check."""
        if not self._flashcards or self._cursor >= len(self._flashcards):
            return None
        return self._flashcards[self._cursor]

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_entry_id(self, entry_id: int | None) -> None:
        """Set the target entry whose flashcards to show. ``None`` clears the tab and invalidates any
        in-flight fetch — used by the parent when transitioning out of ``LINKED_FLASHCARDS``.

        Idempotent: a call with the same id we already hold is a no-op (no refetch). The cursor and
        window reset on a real change; search persists across entry-id changes since it's a per-tab
        preference.
        """
        if entry_id == self._entry_id:
            return

        self._entry_id = entry_id
        self._cursor = 0

        if entry_id is None:
            # No target — wipe the window inline. Bump the fetch id so any in-flight task arriving
            # later observes the mismatch and gets discarded.
            self._fetch_id += 1
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

        Shares ``_query_window`` with ``_fetch`` (same query, different offset). Captures the current
        ``_fetch_id`` synchronously and gates the append on ``_still_current`` so a concurrent
        ``set_entry_id`` / ``set_search`` doesn't leave us extending the new window with stale tail
        rows.
        """
        if self._is_loading or not self._has_more or self._entry_id is None:
            return

        my_id = self._fetch_id
        kwargs = self._query_kwargs()
        more = await self._query_window(kwargs, offset=len(self._flashcards))
        if not self._still_current(my_id):
            return

        self._flashcards.extend(more)
        if len(more) < self._limit:
            self._has_more = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Fetch machinery (mirrors BrowserTabViewModel, sans debounce)
    # ------------------------------------------------------------------

    def _still_current(self, my_id: int) -> bool:
        """True iff the captured fetch id still matches the latest. Used internally to gate
        ``_process_fetched_data``; ``load_more`` uses it to gate its own append."""
        return my_id == self._fetch_id

    def _query_kwargs(self) -> dict[str, Any]:
        """Snapshot the DB-query inputs into a plain dict. Captured synchronously at the call site
        (no await inside) so all queries derived from one snapshot see locally-consistent state, even
        if mutators run between the snapshot and the eventual query.

        Shared by ``_fetch`` (full reload) and ``load_more`` (append) so they always agree on what
        the "current query" is."""
        return {
            "entry_id": self._entry_id,
            "search": self._search or None,
        }

    async def _query_window(
        self, kwargs: dict[str, Any], offset: int,
    ) -> list[Flashcard]:
        """Run the windowed SELECT at ``offset`` against a captured kwargs snapshot. Caller must
        ensure ``kwargs['entry_id']`` is not None."""
        async with self._session_factory() as session:
            return await list_flashcards_for_entry(
                session,
                kwargs["entry_id"],
                search=kwargs["search"],
                limit=self._limit,
                offset=offset,
            )

    def _request_fetch(self) -> None:
        """Bump the fetch id and spawn a new task. Any in-flight task continues running but its
        result is discarded via ``_still_current``. See module docstring for why we don't cancel."""
        self._fetch_id += 1
        my_id = self._fetch_id
        self._is_loading = True
        self.emit(self.dirty)
        asyncio.create_task(self._run_fetch(my_id))

    async def _run_fetch(self, my_id: int) -> None:
        """Wrap ``_fetch`` with the fetch-identity guard and ``is_loading`` bookkeeping. Mirrors the
        base class's ``_debounced_fetch`` minus the debounce phase."""
        try:
            result = await self._fetch()
        except Exception:
            _logger.exception(
                "LinkedFlashcardsPanelViewModel._fetch raised; tab will remain in error "
                "state until next entry-id / search change",
            )
            if self._still_current(my_id):
                self._is_loading = False
                self.emit(self.dirty)
            return

        if not self._still_current(my_id):
            return

        self._is_loading = False
        self._process_fetched_data(result)
        self.emit(self.dirty)

    async def _fetch(self) -> tuple[list[Flashcard], int]:
        """Reload the window + total against the current entry id and search. Stateless: returns
        ``(rows, total)`` for ``_process_fetched_data`` to apply.

        Returns ``([], 0)`` if the entry id is None at task start — possible if ``set_entry_id(None)``
        snuck in before the task ran. The base class's staleness gate would also discard the result,
        but short-circuiting here avoids issuing a query against a null id."""
        kwargs = self._query_kwargs()
        if kwargs["entry_id"] is None:
            return [], 0
        rows = await self._query_window(kwargs, offset=0)
        async with self._session_factory() as session:
            total = await count_flashcards_for_entry(
                session, kwargs["entry_id"], search=kwargs["search"],
            )
        return rows, total

    def _process_fetched_data(
        self, result: tuple[list[Flashcard], int],
    ) -> None:
        """Apply a ``_fetch`` result: replace the window, set the total, reconcile ``_has_more``, and
        clamp the cursor."""
        rows, total = result
        self._flashcards = rows
        self._total = total
        self._has_more = len(self._flashcards) < self._total
        if self._cursor >= len(self._flashcards):
            self._cursor = max(0, len(self._flashcards) - 1)
