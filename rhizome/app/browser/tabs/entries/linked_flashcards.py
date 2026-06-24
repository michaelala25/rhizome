"""Sub-VM for the right-hand flashcard panel in ``State.LINKED_FLASHCARDS``.

Two display modes:

  * **non-relink** — just ``_linked_flashcards`` (flashcards linked to the current entry-id set).
    Cursor indexes directly into it.
  * **relink** — ``_linked_flashcards`` (pinned) + boundary + ``_remaining_flashcards`` (the
    topic-filtered, deduped, paginated pool of other flashcards). Cursor indexes into the
    combined display ``[*linked, <boundary>, *remaining]``; ``cursor_section()`` resolves which.

The pinned section is frozen at fetch time: toggling a pinned row off marks it for unlink but
does not reposition the row. Keeps the partition stable across selection edits so accept can
diff against the originally-linked baseline.

Two independent drivers
-----------------------
The VM owns two independent ``rhizome.app.query`` drivers:

  * ``_linked`` — a ``Query`` keyed on ``(entry_ids, effective-search)``. The pinned section.
  * ``_pool``   — a ``PagedQuery`` keyed on ``(topics, search)``. The relink pool.

Crucially, the pool query is **independent of entry_ids**: it does NOT exclude the linked rows
at the SQL layer. Dedup is a render projection (see ``remaining_flashcards``). This makes the
pool cache stable across entry navigation — the same ``(topics, search)`` window survives entry
changes — and removes the linked → pool data dependency that previously required a synchronous
input snapshot inside ``_fetch``.

Two ``total``s, by design:
  * **raw pool total** — ``_pool.current.total``, the unfiltered count. Drives ``has_more``.
  * **deduped total** — what ``remaining_total`` surfaces: raw minus the linked rows already
    loaded into the window. Exact for the loaded portion, a slight over-estimate for unloaded
    pages — read as "~X" once we surface that distinction at the view layer.

Relink commit (``accept_relink``): diff ``_relink_selected_ids`` against the baseline (live
linked ids) → ``link_flashcards_to_entry`` / ``unlink_flashcards_from_entry`` → commit → drop
the linked cache and re-submit the pinned query. The pool cache is left alone: link membership
changes do not change which rows match ``(topics, search)``, only which dedup out.
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

from rhizome.app.browser.shared.searchable import SearchableModelMixin
from rhizome.app.query import PagedQuery, Query, QueryState

_logger = get_logger("browser.linked_flashcards")

# Pool-window cap; mirrors ``EntryTabModel.DEFAULT_PAGE_LIMIT``. The linked section is unbounded
# (per-entry flashcard counts are usually small) and not paginated.
DEFAULT_PAGE_LIMIT = 500

# Param tuple shapes for the two drivers.
LinkedParams = tuple[tuple[int, ...], str]                  # (entry_ids, effective-search)
PoolParams   = tuple[frozenset[int] | None, str]            # (topics, search)


# TODO: wire ``reload()`` into the panel's ``DatabaseCommitted`` path. Today nothing fires it, so
# flashcard CRUD that happens outside this panel (creates / deletes / edits in another pane) will
# leave stale rows in both caches until something else invalidates them. In the entries tab's parent
# widget, on a ``DatabaseCommitted`` event whose ``tables`` touch ``flashcards`` or
# ``flashcard_entry``, call ``self._linked_flashcards.reload()``.
class LinkedFlashcardsPanelModel(SearchableModelMixin):
    """Sub-VM driving the linked-flashcards panel. See module docstring."""

    def __init__(
        self,
        session_factory: Any,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._limit = limit

        # Target entry ids; empty wipes the pinned section and forbids pool fetches (used when the
        # parent transitions away from LINKED_FLASHCARDS or has an empty multi-select).
        self._entry_ids: frozenset[int] = frozenset()

        # Topic filter pushed down from the parent tab. ``None`` = no filter, empty frozenset =
        # "no rows match". Scopes the pool only; the pinned section ignores it.
        self._topics: frozenset[int] | None = None

        # Active search query (empty = no filter). In non-relink filters the pinned section; in
        # relink filters the pool only — the pinned section stays unconditional so the partition
        # keeps its meaning mid-search.
        self._search: str = ""

        # Index into the combined display ``[*linked, <boundary>, *remaining]`` in relink, or just
        # ``linked`` outside. The boundary sits at index ``len(linked)`` in relink.
        self._cursor: int = 0

        # Relink state. Selection seeds from the linked ids on entry and reseeds after every linked
        # rebase (see ``_linked_changed``): "currently linked → stay linked by default". Pool rows
        # start unselected.
        self._relink_mode: bool = False
        self._relink_selected_ids: set[int] = set()

        # Drivers. Both cached: entry navigation should restore previously-viewed state instantly,
        # and the commit-notification path (``reload``) invalidates on its own when contents change.
        self._linked: Query[LinkedParams, list[Flashcard]] = Query(
            fetch=self._fetch_linked,
            cache_key=lambda p: p,
            on_change=self._linked_changed,
            # Small query, practically instant - the debounce is only useful for cancelling in-flight
            # queries before they run when a new fetch is requested, but here since the queries are so
            # light, the lost work is negligible.
            #
            # NOTE: If users end up having knowledge entries linked to hundreds/thousands of flashcards
            # later down the line (unlikely), we can reconsider a debounce here.
            debounce=0
        )
        self._pool: PagedQuery[PoolParams, Flashcard] = PagedQuery(
            fetch_page=self._fetch_pool_page,
            count=self._fetch_pool_count,
            cache_key=lambda p: p,
            page_size=limit,
            on_change=self._pool_changed,
        )

    # ------------------------------------------------------------------
    # Param snapshots
    # ------------------------------------------------------------------

    def _linked_params(self) -> LinkedParams:
        # In relink the pinned section is unconditional (search filters the pool only), so search
        # drops out of the linked params there.
        return (tuple(sorted(self._entry_ids)), "" if self._relink_mode else self._search)

    def _pool_params(self) -> PoolParams:
        return (self._topics, self._search)

    # ------------------------------------------------------------------
    # Fetch implementations
    # ------------------------------------------------------------------

    async def _fetch_linked(self, params: LinkedParams) -> list[Flashcard]:
        entry_ids, search = params
        if not entry_ids:
            return []
        async with self._session_factory() as session:
            return await list_flashcards_for_entries(
                session, list(entry_ids), search=search or None, limit=self._limit, offset=0,
            )

    async def _fetch_pool_page(
        self, params: PoolParams, offset: int, limit: int,
    ) -> list[Flashcard]:
        topics, search = params
        async with self._session_factory() as session:
            return await list_flashcards_paginated(
                session, topic_ids=topics, search=search or None, limit=limit, offset=offset,
            )

    async def _fetch_pool_count(self, params: PoolParams) -> int:
        topics, search = params
        async with self._session_factory() as session:
            return await count_flashcards(session, topic_ids=topics, search=search or None)

    # ------------------------------------------------------------------
    # Driver change hooks
    # ------------------------------------------------------------------

    def _linked_changed(self) -> None:
        # Relink baseline IS the pinned section -> reseed the selection on every rebase (entry
        # change in relink, or rebase after accept). "currently linked -> stay linked."
        #
        # NOTE: this reseed fires on every READY transition, including cache-hit restores. That's
        # the right behavior today — the baseline for relink edits should be "what's pinned for
        # the current entry, right now". But it means any code path that programmatically pre-edits
        # ``_relink_selected_ids`` *before* the linked query lands will have its edits clobbered
        # by a cache-hit reseed. Not currently a problem (no such caller exists), but if one ever
        # gets added, this branch is where you'd gate it.
        if self._relink_mode and self._linked.state is QueryState.READY:
            self._relink_selected_ids = {fc.id for fc in self.linked_flashcards}
        self._clamp_cursor()
        self.emit(self.Callbacks.OnDirty)

    def _pool_changed(self) -> None:
        self._clamp_cursor()
        self.emit(self.Callbacks.OnDirty)

    def _clamp_cursor(self) -> None:
        total = self.display_row_count()
        if total == 0:
            self._cursor = 0
        elif self._cursor >= total:
            self._cursor = total - 1

    def _linked_ids(self) -> set[int]:
        return {fc.id for fc in self.linked_flashcards}

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def entry_ids(self) -> frozenset[int]:
        """Target entry ids. Empty when the parent is in ``ENTRIES``, in multi-select with no
        entries selected, or pre-bootstrap."""
        return self._entry_ids

    @property
    def topic_filter(self) -> frozenset[int] | None:
        return self._topics

    @property
    def linked_flashcards(self) -> list[Flashcard]:
        """Pinned section. Live reference — callers must not mutate."""
        return self._linked.result or []

    @property
    def remaining_flashcards(self) -> list[Flashcard]:
        """Pool minus anything already pinned above — a pure render projection, no query. Empty
        outside relink. A page of ``limit`` rows may render shorter once the linked rows are
        removed."""
        # NOTE: recomputed on every read. ``_pool.current.rows`` accumulates monotonically across
        # ``load_more`` calls within a single ``(topics, search)`` param set, so cost scales with
        # how deep the user has paged. Trivial at typical scale; if deep pagination + heavy refresh
        # load (e.g., per-keystroke search) ever becomes a real workload, swap this for a lazy
        # ``_remaining_cache: list[Flashcard] | None`` cleared in ``_linked_changed`` /
        # ``_pool_changed`` and recomputed on first read.
        if not self._relink_mode or self._pool.current is None:
            return []
        linked_ids = self._linked_ids()
        return [fc for fc in self._pool.current.rows if fc.id not in linked_ids]

    @property
    def remaining_total(self) -> int | None:
        """Deduped total estimate: raw pool count minus the linked rows already loaded into the
        window. Exact for the loaded portion; a slight over-estimate before the pool is fully paged."""
        if not self._relink_mode or self._pool.current is None:
            return None
        raw = self._pool.current.total
        if raw is None:
            return None
        overlap = sum(1 for fc in self._pool.current.rows if fc.id in self._linked_ids())
        return max(0, raw - overlap)

    @property
    def remaining_has_more(self) -> bool:
        return (
            self._relink_mode
            and self._pool.current is not None
            and self._pool.current.has_more
        )

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
        """Live reference to the relink selection. Callers must not mutate — use the mutators."""
        return self._relink_selected_ids

    @property
    def is_loading(self) -> bool:
        loading = {QueryState.LOADING, QueryState.SLOW}
        if self._linked.state in loading:
            return True
        return self._relink_mode and self._pool.state in loading

    def is_relink_selected(self, flashcard_id: int) -> bool:
        return flashcard_id in self._relink_selected_ids

    def _relink_baseline_ids(self) -> set[int]:
        # Baseline is the live pinned section's ids; reseeded by ``_linked_changed`` on every
        # rebase, so this method just reads the current pinned set.
        return self._linked_ids()

    @property
    def is_relink_dirty(self) -> bool:
        """True iff the relink selection diverges from the baseline. Drives Accept/Cancel
        visibility. Always False outside relink."""
        if not self._relink_mode:
            return False
        return self._relink_selected_ids != self._relink_baseline_ids()

    def cursor_section(self) -> Literal["linked", "boundary", "remaining", "empty"]:
        """Which display section the cursor sits in. In relink the boundary is always painted at
        index ``len(linked)`` (even when both sections are empty), so check relink first."""
        if self._relink_mode:
            n_linked = len(self.linked_flashcards)
            if self._cursor < n_linked:
                return "linked"
            if self._cursor == n_linked:
                return "boundary"
            return "remaining"
        if not self.linked_flashcards:
            return "empty"
        return "linked"

    @property
    def cursor_flashcard(self) -> Flashcard | None:
        """Flashcard under the cursor, or ``None`` on the boundary / empty display."""
        section = self.cursor_section()
        linked = self.linked_flashcards
        if section == "linked":
            if 0 <= self._cursor < len(linked):
                return linked[self._cursor]
            return None
        if section == "remaining":
            idx = self._cursor - (len(linked) + 1)        # +1 for boundary in relink mode
            remaining = self.remaining_flashcards
            if 0 <= idx < len(remaining):
                return remaining[idx]
        return None

    def display_row_count(self) -> int:
        """Total rendered rows. In relink: linked + boundary + remaining. The boundary is drawn
        even when the pool is empty so the partition is always visible."""
        linked = len(self.linked_flashcards)
        if self._relink_mode and self.remaining_flashcards:
            return linked + 1 + len(self.remaining_flashcards)
        if self._relink_mode:
            return linked + 1
        return linked

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_entry_ids(self, entry_ids: Iterable[int]) -> None:
        """Replace the target entry-id set. Idempotent. Empty re-submits the linked query (which
        short-circuits to ``[]``). The pool driver is untouched — entry navigation does not
        re-fetch the pool."""
        new = frozenset(entry_ids)
        if new == self._entry_ids:
            return
        self._entry_ids = new
        self._cursor = 0
        self._linked.submit(self._linked_params())

    def set_topic_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Replace the pool's topic filter. Idempotent. ``None`` = no filter; empty frozenset =
        "no rows" (legal terminal state). Refetches only in relink (the pinned section ignores
        the filter); outside relink the value is stashed for the next relink entry."""
        new: frozenset[int] | None = None if topic_ids is None else frozenset(topic_ids)
        if new == self._topics:
            return
        self._topics = new
        if self._relink_mode and self._entry_ids:
            self._pool.set_params(self._pool_params())
        else:
            self.emit(self.Callbacks.OnDirty)

    def set_search(self, query: str) -> None:
        """Replace the active question/answer search. In non-relink filters the pinned section;
        in relink filters the pool only. Resets the cursor; refetches if a target is loaded."""
        new = query or ""
        if new == self._search:
            return
        self._search = new
        self._cursor = 0
        if not self._entry_ids:
            self.emit(self.Callbacks.OnDirty)
            return
        if self._relink_mode:
            self._pool.set_params(self._pool_params())        # search filters the pool in relink
        else:
            self._linked.submit(self._linked_params())        # search filters the pinned section

    def set_cursor(self, index: int) -> None:
        """Move the row cursor, clamped to the display. The equality early-return prevents the
        feedback loop with the view's programmatic ``move_cursor`` on rebuild (which fires
        another ``RowHighlighted`` and re-enters this method)."""
        total = self.display_row_count()
        new = 0 if total == 0 else max(0, min(index, total - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self.emit(self.Callbacks.OnDirty)

    def enter_relink_mode(self) -> None:
        """Turn on relink, seed the selection from the current linked ids, and ask the pool
        driver to land. Idempotent. If the pinned section was filtered by search, refetch it
        unfiltered — in relink the pinned section is unconditional."""
        if self._relink_mode:
            return
        self._relink_mode = True
        self._relink_selected_ids = self._linked_ids()
        if self._entry_ids:
            if self._search:
                self._linked.submit(self._linked_params())    # pinned drops the search filter
            self._pool.set_params(self._pool_params())
        self.emit(self.Callbacks.OnDirty)

    def exit_relink_mode(self) -> None:
        """Turn off relink and re-acquire the pinned section's search filter if any. Idempotent.
        The pool's window stays in its cache, ready for the next relink entry."""
        if not self._relink_mode:
            return
        self._relink_mode = False
        self._relink_selected_ids.clear()
        if self._entry_ids and self._search:
            self._linked.submit(self._linked_params())        # pinned re-acquires the search filter
        self._clamp_cursor()
        self.emit(self.Callbacks.OnDirty)

    def toggle_current_relink_selection(self) -> None:
        """Flip the cursor flashcard's relink-set membership. No-op outside relink, on the
        boundary, or on an empty display."""
        fc = self.cursor_flashcard
        if not self._relink_mode or fc is None:
            return
        if fc.id in self._relink_selected_ids:
            self._relink_selected_ids.remove(fc.id)
        else:
            self._relink_selected_ids.add(fc.id)
        self.emit(self.Callbacks.OnDirty)

    async def accept_relink(self) -> None:
        """Commit the relink diff (link adds + unlink removes) on ``FlashcardEntry``, drop the
        linked cache, and force a rebase of the pinned section. The pool cache is preserved:
        link membership changes don't change ``(topics, search)`` matching, only what dedups
        out at render time.

        Single-select precondition: ``_entry_ids`` must hold exactly one id. Violated invariant
        logs a warning and bails — never touches the DB."""
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

        self._linked.invalidate(predicate=lambda k: k[0] == (entry_id,))
        self._relink_mode = False
        self._relink_selected_ids.clear()
        self._linked.submit(self._linked_params())            # force rebase of the pinned section
        self.emit(self.Callbacks.OnDirty)

    def cancel_relink(self) -> None:
        """Revert the selection to the baseline. Stays in relink so the user can keep editing."""
        if not self.is_relink_dirty:
            return
        self._relink_selected_ids = self._relink_baseline_ids()
        self.emit(self.Callbacks.OnDirty)

    async def load_more(self) -> None:
        """Append the next page of the pool. No-op outside relink, when no target is loaded, or
        when nothing further is available. ``PagedQuery`` guards re-entry and applies its own
        staleness gate so a concurrent supersede doesn't extend the new window with stale rows."""
        if not self._relink_mode or not self._entry_ids:
            return
        await self._pool.load_more()

    def reload(self) -> None:
        """Commit-notification hook: drop caches and re-run the live queries. Wire to the panel's
        ``DatabaseCommitted`` path when committed tables touch ``flashcards`` / ``flashcard_entry``."""
        self._linked.invalidate()
        self._pool.invalidate()
        if self._entry_ids:
            self._linked.submit(self._linked_params())
        if self._relink_mode and self._entry_ids:
            self._pool.set_params(self._pool_params())
        self.emit(self.Callbacks.OnDirty)
