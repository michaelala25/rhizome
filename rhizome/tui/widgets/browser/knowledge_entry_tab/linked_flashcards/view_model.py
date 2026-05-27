"""LinkedFlashcardsPanelViewModel — sub-VM for the right-hand flashcard table shown when the parent
tab is in ``State.LINKED_FLASHCARDS``.

The panel has two display modes the view branches on:

  * **non-relink** — shows just the flashcards linked to the current entry-id set. The classic
    cursor-driven sub-VM. ``_linked_flashcards`` holds the rows; the cursor indexes into it.
  * **relink** — shows the same linked rows pinned at the top, then (after a boundary row the view
    paints) all other flashcards within the parent tab's topic filter, deduped against the linked
    set. The pool is windowed + paginated. ``_remaining_flashcards`` holds the pool rows; the
    cursor indexes into the combined display ``(linked, [boundary], remaining)``.

The "pinned linked" rows are frozen at fetch time — toggling them in the relink set doesn't
reposition them. The user explicitly opted into this so the partition is stable across selection
edits, and the eventual commit-relink action can diff against the original.

Lifecycle
---------
The parent feeds two things via mutators: ``set_entry_ids`` (the union of entry ids whose linked
flashcards to pin) and ``set_topic_filter`` (the topic-scope for the remaining pool — pushed down
from the parent tab's own topic filter). When the parent transitions *away* from
``LINKED_FLASHCARDS`` it pushes ``set_entry_ids(frozenset())`` to clear the window and invalidate
any in-flight fetch; on transitioning *back* it pushes the current selection. The VM itself
doesn't read the parent state; it just reacts to whatever it's given.

Fetch protocol
--------------
Inherits the debounce + fetch-id staleness gating from ``QueryBackedViewModel``. The earlier
"no debounce, queries are sub-ms" assumption broke once the pool query landed (potentially
thousands of rows, joined to the session table, search across question + answer). The default
50ms window matches the entry tab and absorbs bursts of entry-cursor scrolling without feeling
laggy on a deliberate change.

``_fetch`` runs the linked query first, then the pool query keyed off the resulting linked id
list (passed as ``exclude_ids``). Two sessions because of the data dependency, but each is a
single windowed SELECT + COUNT — well under the cost we'd save by trying to bundle them.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

from rhizome.db import Flashcard
from rhizome.db.operations import (
    count_flashcards,
    link_flashcards_to_entry,
    list_flashcards_for_entries,
    list_flashcards_paginated,
    unlink_flashcards_from_entry,
)
from rhizome.logs import get_logger

from ....query_backed_view_model import QueryBackedViewModel
from ....search_input import SearchableViewModelMixin

_logger = get_logger("browser.linked_flashcards")

# Mirrors ``KnowledgeEntryBrowserTabViewModel.DEFAULT_PAGE_LIMIT`` so the remaining pool shares a
# memory + render footprint cap with the entries tab. The linked section is unbounded in
# principle (per-entry flashcard counts are usually small single digits) and not paginated.
DEFAULT_PAGE_LIMIT = 500


class LinkedFlashcardsPanelViewModel(QueryBackedViewModel, SearchableViewModelMixin):
    """Sub-VM driving the linked-flashcards table.

    Owns: the current target entry id set, the topic filter (pushed down from the parent tab),
    the loaded linked + remaining windows + remaining total/has-more, the search query, the row
    cursor, and the relink-mode selection. All mutators are sync — they either schedule a
    debounced refetch (``_request_fetch``) or update local state and emit ``dirty``.
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

        # The frozenset of entry ids whose linked flashcards we pin at the top. Empty at boot and
        # whenever the parent transitions away from ``LINKED_FLASHCARDS`` — in that state both
        # sections are forced empty and no fetch runs.
        self._entry_ids: frozenset[int] = frozenset()

        # Topic filter pushed down from the parent tab. Same semantics as
        # ``BrowserTabViewModel.set_topic_filter``: ``None`` = no filter, frozenset = restrict
        # remaining pool to those topics, empty frozenset = explicit "no rows match". Only
        # affects the remaining-pool query; the linked section is always shown.
        self._topic_filter: frozenset[int] | None = None

        # Linked (pinned) section. Frozen at fetch time — toggling relink-set membership doesn't
        # reposition rows. In non-relink mode this is the only displayed section.
        self._linked_flashcards: list[Flashcard] = []

        # Remaining pool (relink mode only). Empty list outside relink. Paginated:
        # ``_remaining_total`` is None until the first count lands, ``_remaining_has_more`` flips
        # when the loaded window doesn't cover the full result set.
        self._remaining_flashcards: list[Flashcard] = []
        self._remaining_total: int | None = None
        self._remaining_has_more: bool = False

        # Search query. In non-relink, filters the linked section (existing behaviour). In relink,
        # filters the remaining pool only — the linked section stays unconditionally pinned so
        # the "originally linked" partition stays meaningful even mid-search. Empty string = no
        # filter.
        self._search: str = ""

        # Row cursor: an index into the combined display list as the view renders it. In
        # non-relink, the display is just ``_linked_flashcards`` and the cursor indexes directly.
        # In relink, the display is ``[*linked, <boundary>, *remaining]`` — index ``len(linked)``
        # is the boundary row (no flashcard; toggle no-ops there).
        self._cursor: int = 0

        # Relink mode. When True the table renders a "sel" column with the entries-style
        # darkened palette, ``_relink_selected_ids`` tracks the user's pick, and the broader
        # pool query runs. Selection-by-default semantic: every linked row is selected on entry,
        # representing "stay linked"; toggling a linked row off marks it for unlink; toggling a
        # remaining row on marks it for link. Reseed happens on every successful fetch while in
        # mode (so cursor-moves on the entry side hand the user a fresh "all originally linked"
        # baseline for the new entry).
        self._relink_mode: bool = False
        self._relink_selected_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def entry_ids(self) -> frozenset[int]:
        """The frozenset of entry ids whose linked flashcards are pinned. Empty when the parent
        is in ``ENTRIES`` state, in multi-select with no entries selected, or pre-bootstrap."""
        return self._entry_ids

    @property
    def topic_filter(self) -> frozenset[int] | None:
        return self._topic_filter

    @property
    def linked_flashcards(self) -> list[Flashcard]:
        """The pinned section. Live reference — callers must not mutate."""
        return self._linked_flashcards

    @property
    def remaining_flashcards(self) -> list[Flashcard]:
        """The paginated pool section. Empty list outside relink mode."""
        return self._remaining_flashcards

    @property
    def remaining_total(self) -> int | None:
        return self._remaining_total

    @property
    def remaining_has_more(self) -> bool:
        return self._remaining_has_more

    @property
    def search(self) -> str:
        return self._search

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def relink_mode(self) -> bool:
        return self._relink_mode

    @property
    def relink_selected_ids(self) -> set[int]:
        """Live reference to the relink selection set. Callers must not mutate it — use the
        mutators below."""
        return self._relink_selected_ids

    def is_relink_selected(self, flashcard_id: int) -> bool:
        return flashcard_id in self._relink_selected_ids

    def _relink_baseline_ids(self) -> set[int]:
        """The "originally-linked" ids — the set the selection is compared against to compute
        ``is_relink_dirty``. By construction these are the ids of the currently-linked
        flashcards (the pinned section); reseeded on every successful fetch."""
        return {fc.id for fc in self._linked_flashcards}

    @property
    def is_relink_dirty(self) -> bool:
        """True when the relink selection diverges from the originally-linked set. Drives
        visibility of the Accept/Cancel choices widget. False outside relink mode regardless of
        the selection contents — the dialog has no meaning when relink is off."""
        if not self._relink_mode:
            return False
        return self._relink_selected_ids != self._relink_baseline_ids()

    def cursor_section(self) -> Literal["linked", "boundary", "remaining", "empty"]:
        """Which display section the cursor currently sits in. Drives view-side gating
        (boundary-row no-op toggle, status formatting) without forcing the caller to recompute
        the section bounds.

        Mirrors the view's render order: in relink the boundary row is always painted at index
        ``len(linked)`` (even when both sections are empty — the partition still exists
        conceptually and the view shows a single divider line), so the relink case is checked
        first."""
        if self._relink_mode:
            n_linked = len(self._linked_flashcards)
            if self._cursor < n_linked:
                return "linked"
            if self._cursor == n_linked:
                return "boundary"
            return "remaining"
        if not self._linked_flashcards:
            return "empty"
        return "linked"

    @property
    def cursor_flashcard(self) -> Flashcard | None:
        """The flashcard currently under the cursor, or ``None`` when on the boundary row or the
        display is empty. Convenience for the answer-preview widget."""
        section = self.cursor_section()
        if section == "linked":
            if 0 <= self._cursor < len(self._linked_flashcards):
                return self._linked_flashcards[self._cursor]
            return None
        if section == "remaining":
            offset = len(self._linked_flashcards) + 1  # +1 for boundary in relink mode
            idx = self._cursor - offset
            if 0 <= idx < len(self._remaining_flashcards):
                return self._remaining_flashcards[idx]
        return None

    def display_row_count(self) -> int:
        """Total rows the view will render: linked rows + (boundary + remaining) when in relink
        mode. Used by the view to size its row signature and by the cursor clamp."""
        if self._relink_mode and self._remaining_flashcards:
            return len(self._linked_flashcards) + 1 + len(self._remaining_flashcards)
        if self._relink_mode:
            # Relink with no remaining yet (still loading or genuinely empty pool): still paint
            # a boundary row so the user sees the partition structure.
            return len(self._linked_flashcards) + 1
        return len(self._linked_flashcards)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_entry_ids(self, entry_ids: Iterable[int]) -> None:
        """Replace the target set of entry ids whose linked flashcards to pin. Pass
        ``frozenset()`` (or any empty iterable) to wipe both sections and invalidate any in-flight
        fetch — used by the parent when transitioning out of ``LINKED_FLASHCARDS`` and when
        multi-select is on with nothing selected.

        Idempotent on an unchanged set: no refetch. A real change resets the cursor; search +
        topic-filter persist across set changes since they're per-tab preferences.
        """
        new = frozenset(entry_ids)
        if new == self._entry_ids:
            return

        self._entry_ids = new
        self._cursor = 0

        if not new:
            # No target — wipe both sections inline. Bump the fetch id so any in-flight task
            # arriving later observes the mismatch and gets discarded.
            self._fetch_id += 1
            self._linked_flashcards = []
            self._remaining_flashcards = []
            self._remaining_total = 0
            self._remaining_has_more = False
            self._is_loading = False
            self.emit(self.dirty)
            return

        self._request_fetch()

    def set_topic_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Replace the topic filter for the remaining pool. Pushed down from the parent tab so
        the panel scopes its pool to the same tree selection the entry table shows.

        Idempotent on an unchanged filter. ``None`` and ``frozenset()`` are distinct: None means
        "no filter" (every topic); empty frozenset means "explicitly nothing matches" (legal
        terminal state). Refetches when relink mode is on (the pool query depends on it); when
        relink is off the topic filter still gets stored but no refetch is needed since the
        linked section doesn't depend on it.
        """
        new: frozenset[int] | None = None if topic_ids is None else frozenset(topic_ids)
        if new == self._topic_filter:
            return
        self._topic_filter = new
        if self._relink_mode and self._entry_ids:
            self._request_fetch()
        else:
            # Stash the filter but skip the refetch — the linked section doesn't use it. When the
            # user eventually enters relink, the next fetch picks it up automatically.
            self.emit(self.dirty)

    def set_search(self, query: str) -> None:
        """Replace the active question/answer search.

        In non-relink mode, filters the linked section (existing behaviour). In relink mode,
        filters the remaining pool only — the linked section stays unconditionally pinned so the
        "originally linked" partition keeps its meaning. Resets the cursor and refetches if an
        entry-id set is loaded.
        """
        new = query or ""
        if new == self._search:
            return

        self._search = new
        self._cursor = 0

        if not self._entry_ids:
            self.emit(self.dirty)
            return

        self._request_fetch()

    def set_cursor(self, index: int) -> None:
        """Move the row cursor. Clamped to the combined display range. Emits ``dirty`` so the
        table repaints and the answer preview re-reads ``cursor_flashcard``.

        Repaint includes a programmatic ``move_cursor`` on the rebuild path, which fires another
        ``DataTable.RowHighlighted`` and re-enters this method via
        ``on_data_table_row_highlighted``. That second call is a no-op thanks to the
        index-equality guard below.
        """
        total = self.display_row_count()
        if total == 0:
            new = 0
        else:
            new = max(0, min(index, total - 1))

        if new == self._cursor:
            return

        self._cursor = new
        self.emit(self.dirty)

    def enter_relink_mode(self) -> None:
        """Turn on relink mode and refetch so the broader pool comes in. Selection seeds from the
        currently-loaded linked ids — those represent "stay linked" — and reseeds on every
        successful fetch while in mode (see ``_process_fetched_data``). Idempotent."""
        if self._relink_mode:
            return
        self._relink_mode = True
        self._relink_selected_ids = {fc.id for fc in self._linked_flashcards}
        # Pool query is now in scope — refetch to bring it in.
        if self._entry_ids:
            self._request_fetch()
        else:
            self.emit(self.dirty)

    def exit_relink_mode(self) -> None:
        """Turn off relink mode, discard the selection + remaining pool, and refetch the (now
        cheaper) linked-only query. Idempotent."""
        if not self._relink_mode:
            return
        self._relink_mode = False
        self._relink_selected_ids.clear()
        self._remaining_flashcards = []
        self._remaining_total = 0
        self._remaining_has_more = False
        # Clamp the cursor — the display just shrank.
        total = self.display_row_count()
        if total > 0 and self._cursor >= total:
            self._cursor = total - 1
        if self._entry_ids:
            self._request_fetch()
        else:
            self.emit(self.dirty)

    def toggle_current_relink_selection(self) -> None:
        """Flip membership of the cursor's flashcard in the relink set. No-op outside relink
        mode, on the boundary row, or when the display is empty."""
        fc = self.cursor_flashcard
        if not self._relink_mode or fc is None:
            return
        if fc.id in self._relink_selected_ids:
            self._relink_selected_ids.remove(fc.id)
        else:
            self._relink_selected_ids.add(fc.id)
        self.emit(self.dirty)

    async def accept_relink(self) -> None:
        """Commit the current relink selection. Computes the diff against the originally-linked
        baseline, applies it via insert/delete on ``FlashcardEntry``, then exits relink mode
        (which triggers a refetch so the rebased linked section reflects the new DB state).

        Semantically the action "concludes" the relink session — the user has finished editing
        for this entry and wants to move on. ``cancel_relink`` is the path for "never mind,
        keep going". No-op when the selection is clean (the choices widget is hidden in that
        state, so this shouldn't be reachable, but defensive).

        Relink is a single-select-only mode, so ``_entry_ids`` must contain exactly one id; if
        somehow it doesn't (the parent tab's invariant was violated), log and bail without
        touching the DB."""
        if not self.is_relink_dirty:
            return
        if len(self._entry_ids) != 1:
            _logger.warning(
                "accept_relink called with %d entry ids; expected exactly 1. Aborting.",
                len(self._entry_ids),
            )
            return
        entry_id = next(iter(self._entry_ids))
        baseline = self._relink_baseline_ids()
        to_link = self._relink_selected_ids - baseline
        to_unlink = baseline - self._relink_selected_ids

        async with self._session_factory() as session:
            linked = await link_flashcards_to_entry(
                session, entry_id=entry_id, flashcard_ids=to_link,
            )
            unlinked = await unlink_flashcards_from_entry(
                session, entry_id=entry_id, flashcard_ids=to_unlink,
            )
            await session.commit()
        _logger.info(
            "relink accept: linked %d, unlinked %d on entry_id=%d",
            linked, unlinked, entry_id,
        )

        self.exit_relink_mode()

    def cancel_relink(self) -> None:
        """Revert the selection to the originally-linked baseline. Stays in relink mode so the
        user can keep working without re-entering. No-op when already clean."""
        if not self.is_relink_dirty:
            return
        self._relink_selected_ids = self._relink_baseline_ids()
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of remaining-pool flashcards. No-op outside relink mode, if a
        fetch is in flight, the entry-id set is empty, or nothing further is available. Doesn't
        move the cursor.

        Shares ``_query_pool_window`` with ``_fetch``. Captures the current ``_fetch_id``
        synchronously and gates the append on ``_still_current`` so a concurrent ``set_entry_ids``
        / ``set_search`` / ``set_topic_filter`` doesn't leave us extending the new window with
        stale tail rows.
        """
        if (
            not self._relink_mode
            or self._is_loading
            or not self._remaining_has_more
            or not self._entry_ids
        ):
            return

        my_id = self._fetch_id
        linked_ids = [fc.id for fc in self._linked_flashcards]
        more = await self._query_pool_window(
            topic_ids=self._topic_filter,
            exclude_ids=linked_ids,
            search=self._search or None,
            offset=len(self._remaining_flashcards),
        )
        if not self._still_current(my_id):
            return

        self._remaining_flashcards.extend(more)
        if len(more) < self._limit:
            self._remaining_has_more = False
        # Newly-loaded rows in the remaining pool are NOT auto-selected — they represent
        # not-currently-linked flashcards. Their default state is "not linked, not in the
        # relink set". User has to opt them in explicitly.
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _query_kwargs(self) -> dict[str, Any]:
        """Snapshot the DB-query inputs into a plain dict. Captured synchronously at the call
        site (no await inside) so all queries derived from one snapshot see locally-consistent
        state, even if mutators run between the snapshot and the eventual query.

        Shared by ``_fetch`` for consistency; ``load_more`` reads live state instead since by
        the time it runs the snapshot must reflect the user's most recent intent."""
        return {
            "entry_ids": tuple(sorted(self._entry_ids)),
            "topic_ids": (
                None if self._topic_filter is None else tuple(sorted(self._topic_filter))
            ),
            "search": self._search or None,
            "relink": self._relink_mode,
        }

    async def _query_linked(
        self, entry_ids: tuple[int, ...], search: str | None,
    ) -> list[Flashcard]:
        """Linked section query. Search applies in non-relink mode only — relink keeps the
        pinned section unconditional (see ``set_search`` docstring)."""
        if not entry_ids:
            return []
        async with self._session_factory() as session:
            return await list_flashcards_for_entries(
                session, list(entry_ids), search=search, limit=self._limit, offset=0,
            )

    async def _query_pool_window(
        self,
        *,
        topic_ids: frozenset[int] | None | tuple[int, ...],
        exclude_ids: list[int],
        search: str | None,
        offset: int,
    ) -> list[Flashcard]:
        """Remaining-pool window query. Accepts a snapshot ``topic_ids`` (None, frozenset, or
        tuple) and an explicit ``exclude_ids`` list captured from the linked section."""
        async with self._session_factory() as session:
            return await list_flashcards_paginated(
                session,
                topic_ids=topic_ids,
                exclude_ids=exclude_ids,
                search=search,
                limit=self._limit,
                offset=offset,
            )

    async def _query_pool_count(
        self,
        *,
        topic_ids: frozenset[int] | None | tuple[int, ...],
        exclude_ids: list[int],
        search: str | None,
    ) -> int:
        async with self._session_factory() as session:
            return await count_flashcards(
                session,
                topic_ids=topic_ids,
                exclude_ids=exclude_ids,
                search=search,
            )

    # ------------------------------------------------------------------
    # QueryBackedViewModel contract
    # ------------------------------------------------------------------

    async def _fetch(self) -> tuple[list[Flashcard], list[Flashcard], int]:
        """Reload both sections against the current snapshot. Returns
        ``(linked, remaining, remaining_total)``.

        Linked section runs first (we need its ids as the exclusion filter for the pool query).
        In non-relink mode the pool query is skipped — returns ``([], 0)`` for the remaining
        tuple.
        """
        kwargs = self._query_kwargs()
        entry_ids: tuple[int, ...] = kwargs["entry_ids"]
        if not entry_ids:
            return [], [], 0

        # Linked section. Search applies only outside relink — see ``set_search``.
        linked_search = None if kwargs["relink"] else kwargs["search"]
        linked = await self._query_linked(entry_ids, linked_search)

        if not kwargs["relink"]:
            return linked, [], 0

        linked_ids = [fc.id for fc in linked]
        remaining = await self._query_pool_window(
            topic_ids=kwargs["topic_ids"],
            exclude_ids=linked_ids,
            search=kwargs["search"],
            offset=0,
        )
        remaining_total = await self._query_pool_count(
            topic_ids=kwargs["topic_ids"],
            exclude_ids=linked_ids,
            search=kwargs["search"],
        )
        return linked, remaining, remaining_total

    def _process_fetched_data(
        self,
        result: tuple[list[Flashcard], list[Flashcard], int],
    ) -> None:
        """Apply a ``_fetch`` result: replace both windows, reconcile the remaining-pool
        has_more, clamp the cursor, and reseed the relink baseline (linked rows only — pool
        rows default to not-selected)."""
        linked, remaining, remaining_total = result
        self._linked_flashcards = linked
        self._remaining_flashcards = remaining
        self._remaining_total = remaining_total
        self._remaining_has_more = (
            self._relink_mode and len(remaining) < remaining_total
        )

        total = self.display_row_count()
        if total == 0:
            self._cursor = 0
        elif self._cursor >= total:
            self._cursor = total - 1

        # Reseed the relink baseline to the new linked set on every fetch — represents "all
        # currently linked, which by default should remain linked". Pool rows start unselected
        # (they're not currently linked).
        if self._relink_mode:
            self._relink_selected_ids = {fc.id for fc in self._linked_flashcards}
