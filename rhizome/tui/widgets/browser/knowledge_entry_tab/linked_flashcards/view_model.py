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

Lifecycle: the parent pushes ``set_entry_ids`` (target ids; empty wipes both sections + invalidates
in-flight fetches) and ``set_topic_filter`` (scope for the pool). The VM never reads parent state.

Fetch protocol: inherits debounce + fetch-id staleness gating from ``QueryBackedViewModel``.
``_fetch`` runs the linked query first, then the pool query keyed off the resulting linked ids
via ``exclude_ids`` (data dependency, so two sessions). Outside relink the pool query is skipped.
``load_more`` extends the pool only; the pinned section doesn't paginate.

Relink commit (``accept_relink``): diff ``_relink_selected_ids`` against the live baseline
(``_relink_baseline_ids`` = ids of the pinned section) → ``link_flashcards_to_entry`` /
``unlink_flashcards_from_entry`` (both idempotent on the ``FlashcardEntry`` join table) → commit
→ ``exit_relink_mode`` (which re-requests a fetch so the pinned section rebases off the new DB
state). ``cancel_relink`` reverts the selection to the baseline but stays in relink. Relink is
single-select only — ``accept_relink`` asserts ``len(_entry_ids) == 1`` and bails with a warning
log otherwise.
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

from rhizome.app.query_backed_vm import QueryBackedViewModel
from ....search_input import SearchableViewModelMixin

_logger = get_logger("browser.linked_flashcards")

# Pool-window cap; mirrors ``KnowledgeEntryBrowserTabViewModel.DEFAULT_PAGE_LIMIT``. The linked
# section is unbounded (per-entry flashcard counts are usually small) and not paginated.
DEFAULT_PAGE_LIMIT = 500


class LinkedFlashcardsPanelViewModel(QueryBackedViewModel, SearchableViewModelMixin):
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

        # Target entry ids; empty wipes both sections and forbids fetches (used when the parent
        # transitions away from LINKED_FLASHCARDS or has an empty multi-select).
        self._entry_ids: frozenset[int] = frozenset()

        # Topic filter pushed down from the parent tab. Same semantics as the tab-base
        # ``set_topic_filter``: None = no filter, empty frozenset = "no rows match". Scopes the
        # pool only; the pinned section ignores it.
        self._topic_filter: frozenset[int] | None = None

        # Pinned section. Frozen at fetch time. In non-relink the only displayed section.
        self._linked_flashcards: list[Flashcard] = []

        # Pool (relink only). ``_remaining_total`` is None until the first count lands.
        self._remaining_flashcards: list[Flashcard] = []
        self._remaining_total: int | None = None
        self._remaining_has_more: bool = False

        # Active search query (empty = no filter). In non-relink filters the linked section; in
        # relink filters the pool only — the pinned section stays unconditional so the partition
        # keeps its meaning mid-search.
        self._search: str = ""

        # Index into the combined display ``[*linked, <boundary>, *remaining]`` in relink, or
        # just ``linked`` outside. The boundary sits at index ``len(linked)`` in relink.
        self._cursor: int = 0

        # Relink state. Selection seeds from the linked ids on entry and reseeds after every
        # successful fetch (see ``_process_fetched_data``): "currently linked → stay linked by
        # default". Pool rows start unselected; user opts in with ``space``.
        self._relink_mode: bool = False
        self._relink_selected_ids: set[int] = set()

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
        return self._topic_filter

    @property
    def linked_flashcards(self) -> list[Flashcard]:
        """Pinned section. Live reference — callers must not mutate."""
        return self._linked_flashcards

    @property
    def remaining_flashcards(self) -> list[Flashcard]:
        """Paginated pool. Empty outside relink."""
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
        """Live reference to the relink selection. Callers must not mutate — use the mutators."""
        return self._relink_selected_ids

    def is_relink_selected(self, flashcard_id: int) -> bool:
        return flashcard_id in self._relink_selected_ids

    def _relink_baseline_ids(self) -> set[int]:
        # Baseline is *always* the live pinned section's ids — reseeds implicitly on every fetch
        # via the linked-section replacement in ``_process_fetched_data``.
        return {fc.id for fc in self._linked_flashcards}

    @property
    def is_relink_dirty(self) -> bool:
        """True iff the relink selection diverges from the baseline. Drives Accept/Cancel
        visibility. Always False outside relink."""
        if not self._relink_mode:
            return False
        return self._relink_selected_ids != self._relink_baseline_ids()

    def cursor_section(self) -> Literal["linked", "boundary", "remaining", "empty"]:
        """Which display section the cursor sits in. In relink the boundary is always painted
        at index ``len(linked)`` (even when both sections are empty), so check relink first."""
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
        """Flashcard under the cursor, or ``None`` on the boundary / empty display."""
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
        """Total rendered rows. In relink: linked + boundary + remaining. The boundary is drawn
        even when the pool is empty so the partition is always visible."""
        if self._relink_mode and self._remaining_flashcards:
            return len(self._linked_flashcards) + 1 + len(self._remaining_flashcards)
        if self._relink_mode:
            return len(self._linked_flashcards) + 1
        return len(self._linked_flashcards)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_entry_ids(self, entry_ids: Iterable[int]) -> None:
        """Replace the target entry-id set. Idempotent. Empty wipes both sections and bumps the
        fetch id so any in-flight fetch is discarded on arrival. Search + topic-filter persist."""
        new = frozenset(entry_ids)
        if new == self._entry_ids:
            return

        self._entry_ids = new
        self._cursor = 0

        if not new:
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
        """Replace the pool's topic filter. Idempotent. ``None`` = no filter; empty frozenset =
        "no rows" (legal terminal state). Refetches only in relink (the pinned section ignores
        the filter); outside relink the value is stashed for the next relink entry."""
        new: frozenset[int] | None = None if topic_ids is None else frozenset(topic_ids)
        if new == self._topic_filter:
            return
        self._topic_filter = new
        if self._relink_mode and self._entry_ids:
            self._request_fetch()
        else:
            self.emit(self.dirty)

    def set_search(self, query: str) -> None:
        """Replace the active question/answer search. In non-relink filters the linked section;
        in relink filters the pool only. Resets the cursor; refetches if a target is loaded."""
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
        """Move the row cursor, clamped to the display. The equality early-return prevents the
        feedback loop with the view's programmatic ``move_cursor`` on rebuild (which fires
        another ``RowHighlighted`` and re-enters this method)."""
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
        """Turn on relink and refetch so the pool comes in. Idempotent. Selection seeds from the
        current linked ids; subsequent fetches reseed via ``_process_fetched_data``."""
        if self._relink_mode:
            return
        self._relink_mode = True
        self._relink_selected_ids = {fc.id for fc in self._linked_flashcards}
        if self._entry_ids:
            self._request_fetch()
        else:
            self.emit(self.dirty)

    def exit_relink_mode(self) -> None:
        """Turn off relink, discard pool + selection, and refetch the (cheaper) linked-only
        query. Idempotent."""
        if not self._relink_mode:
            return
        self._relink_mode = False
        self._relink_selected_ids.clear()
        self._remaining_flashcards = []
        self._remaining_total = 0
        self._remaining_has_more = False
        total = self.display_row_count()
        if total > 0 and self._cursor >= total:
            self._cursor = total - 1
        if self._entry_ids:
            self._request_fetch()
        else:
            self.emit(self.dirty)

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
        self.emit(self.dirty)

    async def accept_relink(self) -> None:
        """Commit the relink diff (link adds + unlink removes) on ``FlashcardEntry``, then exit
        relink (which refetches and rebases the linked section). ``cancel_relink`` is the
        "never mind, keep going" path.

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

        self.exit_relink_mode()

    def cancel_relink(self) -> None:
        """Revert the selection to the baseline. Stays in relink so the user can keep editing."""
        if not self.is_relink_dirty:
            return
        self._relink_selected_ids = self._relink_baseline_ids()
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of the pool. No-op outside relink, when no target is loaded,
        when a fetch is in flight, or when nothing further is available. ``_still_current`` gate
        discards the result if a concurrent mutator bumped ``_fetch_id`` mid-await."""
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
        # Newly-loaded pool rows are NOT auto-selected (they aren't linked yet).
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _query_kwargs(self) -> dict[str, Any]:
        """Snapshot of DB-query inputs, captured synchronously so two queries from one ``_fetch``
        agree even if a mutator runs between them. ``load_more`` reads live state instead — it
        runs after user intent has shifted, so the latest values are the right ones."""
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
        """Linked section query. Caller passes ``search=None`` in relink to keep the pinned
        section unconditional."""
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
        """Pool window query. ``topic_ids`` may be None / frozenset / tuple; ``exclude_ids``
        is the linked-section ids captured by the caller."""
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
        """Reload both sections. Returns ``(linked, remaining, remaining_total)``. Linked runs
        first because its ids are the pool query's ``exclude_ids``. Pool skipped in non-relink."""
        kwargs = self._query_kwargs()
        entry_ids: tuple[int, ...] = kwargs["entry_ids"]
        if not entry_ids:
            return [], [], 0

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
        """Replace both windows, reconcile has-more, clamp the cursor, and reseed the relink
        selection to the new linked ids (pool rows start unselected)."""
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

        if self._relink_mode:
            self._relink_selected_ids = {fc.id for fc in self._linked_flashcards}
