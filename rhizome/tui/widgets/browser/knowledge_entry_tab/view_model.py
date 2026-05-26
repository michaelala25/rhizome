"""KnowledgeEntryBrowserTabViewModel — the first concrete browser tab.

Shows ``KnowledgeEntry`` rows matching the orchestrator's topic filter (plus its own search / sort /
entry-type state) in a fixed-size window. Total counts and pagination are kept deliberately simple
for the MVP: a single LIMIT-N window with a "showing N of M" hint, and an explicit ``load_more`` for
the next page. Once we want true virtualized scroll, the seam is at ``_query_window`` — swap the
offset-based call for a keyset-paginated one and the rest of the VM keeps working.

Filter, search, and sort are all "reset" operations: changing any of them discards the current window
and refetches from offset 0, resetting the row cursor. ``load_more`` is an "append" operation — it
extends the existing window without touching the cursor.

Scope
-----
This VM owns *data facts*: the loaded window, the current sort/search/filter values, the cursor, the
multi-select toggle and selection set, and the orchestration of bulk actions (delete / change topic /
change type). It does **not** own dialog UI state — which dialog is open, where its cursor lives,
which option is highlighted, focus management. Those concerns live in the view side. The VM exposes
the actions the dialogs eventually invoke (``set_sort``, ``apply_filter``,
``delete_selected_entries``, ``change_topic_on_selected_entries``, ``change_type_on_selected_entries``)
and leaves UI choreography to Textual.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from rhizome.db import KnowledgeEntry
from rhizome.db.models import EntryType
from rhizome.db.operations import (
    EntrySortKey,
    count_entries_filtered,
    delete_entry,
    list_entries_paginated,
    update_entry,
)
from rhizome.logs import get_logger

from ..tab_base import BrowserTabViewModel
from .entry_details import EntryDetailsViewModel
from .linked_flashcards import LinkedFlashcardsPanelViewModel

_logger = get_logger("browser.knowledge_entry_tab")

# Hard cap on the rows fetched per page. See braindump for the rationale: at 100K+ entries we want a
# bounded memory + render footprint, and "showing 500 of N+, load more" is the simplest UX that scales.
# Lifting this is a one-line change; switching to keyset pagination is the longer-term migration.
DEFAULT_PAGE_LIMIT = 500


class KnowledgeEntryBrowserTabViewModel(BrowserTabViewModel):
    """Concrete tab VM for browsing knowledge entries."""

    TITLE = "Knowledge Entries"

    class State(enum.Enum):
        """Top-level layout state for the tab.

        Two states for now — the default ``ENTRIES`` view (entries table + details panel) and
        ``LINKED_FLASHCARDS`` (entries table + per-cursor flashcard table; details panel hidden).
        The view branches its layout on ``vm.state``; the VM owns the transition policy and exposes
        ``transition_to`` as the only mutator.

        Extra states (e.g. an entry-graph view, a topic-resources view) would slot in here and the
        view's layout dispatch without changing the transition contract.
        """

        ENTRIES = "entries"
        LINKED_FLASHCARDS = "linked_flashcards"

    def __init__(
        self,
        session_factory: Any,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        super().__init__(session_factory)
        self._limit = limit

        # Top-level layout state. Boot is always ``ENTRIES``; ``transition_to`` is the only mutator.
        self._state: State = self.State.ENTRIES

        # Result window state. ``_entries`` is the currently-loaded rows; ``_total`` is the count of rows
        # matching the filter (None until the first count-query lands). ``_has_more`` is true when the
        # loaded window doesn't cover the full result set.
        self._entries: list[KnowledgeEntry] = []
        self._total: int | None = None
        self._has_more: bool = False

        # Search / sort / entry-type filter state. ``_search`` is an empty string when no search is
        # active. ``_entry_types`` follows the same None/tuple convention as ``BrowserTabViewModel``'s
        # topic filter: ``None`` = no filter; a tuple restricts to those types; an empty tuple is a legal
        # "no rows match" terminal state. Default sort is ``id`` ascending.
        self._search: str = ""
        self._sort_by: EntrySortKey = "id"
        self._sort_dir: Literal["asc", "desc"] = "asc"
        self._entry_types: tuple[EntryType, ...] | None = None
        # Flashcard-presence filter. ``None`` = no filter; ``True`` = restrict to entries that
        # have at least one linked flashcard; ``False`` = restrict to entries with none. Threaded
        # through the same _query_kwargs path as the other filter axes.
        self._has_flashcards: bool | None = None

        # Row cursor within the currently-loaded window. The view owns navigation; the VM owns the
        # persisted position so it survives repaints. Reset to 0 on any "reset" operation.
        self._cursor: int = 0

        # Multi-select state. When ``_multi_select_active`` is True the view paints a leading marker
        # column ("[x]"/"[ ]") and the user can toggle selection of the cursor's row. ``_selected_ids``
        # is keyed by entry id (not row index) so the selection survives ``load_more`` and refetches.
        # Turning the mode off clears the set ("abandons the selection").
        self._multi_select_active: bool = False
        self._selected_ids: set[int] = set()

        # The detail panel's VM. We push it the cursor's entry via ``_sync_details`` whenever the cursor
        # moves or the window reloads. The tab view picks the VM up via ``self.details`` to construct its
        # companion ``EntryDetailsView``. We subscribe to its ``SAVED`` callback so that after an Accept
        # we can repaint the table row (the in-memory ``KnowledgeEntry`` was mutated in place, but the
        # ``DataTable`` doesn't know that until we emit ``dirty`` here).
        self._details = EntryDetailsViewModel(session_factory)
        self._details.subscribe(self._details.saved, self._on_details_saved)

        # Sub-VM driving the linked-flashcards right-hand table (only rendered in
        # ``State.LINKED_FLASHCARDS``). We feed it the cursor entry id via ``_sync_linked_flashcards``
        # but only while we're actually in that state — see ``_sync_linked_flashcards``'s guard.
        # The view picks the sub-VM up via ``self.linked_flashcards`` to construct its companion
        # ``LinkedFlashcardsPanelView`` (not yet implemented).
        self._linked_flashcards = LinkedFlashcardsPanelViewModel(session_factory)

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def entries(self) -> list[KnowledgeEntry]:
        return self._entries

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
    def sort_by(self) -> EntrySortKey:
        return self._sort_by

    @property
    def sort_dir(self) -> Literal["asc", "desc"]:
        return self._sort_dir

    @property
    def entry_types(self) -> tuple[EntryType, ...] | None:
        """Current entry-type filter. ``None`` means no filter; a tuple restricts to those types; an
        empty tuple means "no rows match" (legal terminal state, mirrors ``BrowserTabViewModel``'s
        topic filter semantics)."""
        return self._entry_types

    @property
    def has_flashcards(self) -> bool | None:
        """Current flashcard-presence filter. ``None`` means no filter; ``True`` restricts to entries
        with at least one linked flashcard; ``False`` restricts to entries with none."""
        return self._has_flashcards

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def details(self) -> EntryDetailsViewModel:
        """Sub-VM driving the entry detail panel. Owned by this tab VM; the view picks it up to construct
        the companion view."""
        return self._details

    @property
    def linked_flashcards(self) -> LinkedFlashcardsPanelViewModel:
        """Sub-VM driving the linked-flashcards table (rendered only in ``State.LINKED_FLASHCARDS``).
        Owned by this tab VM. The tab VM feeds it the cursor entry id via
        ``_sync_linked_flashcards``."""
        return self._linked_flashcards

    @property
    def session_factory(self) -> Any:
        """Exposed so the tab view can hand the same factory off to modal screens (e.g. the topic
        picker) without reaching into the inherited private attr."""
        return self._session_factory

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def selected_ids(self) -> set[int]:
        """Live reference to the selected-id set. Callers must not mutate it — use
        ``toggle_current_selection`` / ``toggle_multi_select`` instead. Matches the trust convention used
        by ``entries`` (also returned by reference)."""
        return self._selected_ids

    def is_selected(self, entry_id: int) -> bool:
        return entry_id in self._selected_ids

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def transition_to(self, new_state: State) -> None:
        """Transition the tab to a new top-level layout state.

        Idempotent: a transition to the current state is a no-op. Otherwise the transition discards
        any unsaved title/content edits in the details panel (matches the silent-discard policy
        already used for cursor-move-while-dirty) and re-seeds the linked-flashcards sub-VM.

        Multi-select state is preserved across transitions — the selection set is keyed by entry id,
        has no visual coupling to the layout state. In-flight fetches are preserved too; the base
        class's fetch-id gating handles any race between an outgoing fetch and the new state.

        Dialog dismissal on state change is the view's concern: the view subscribes to ``dirty`` and
        can drop whichever dialog widget it currently has visible when ``state`` changes.
        """
        if new_state is self._state:
            return
        _logger.info("Tab state transition: %s -> %s", self._state.value, new_state.value)

        # Discard any unsaved title/content edits in the details panel. ``cancel`` no-ops when clean.
        self._details.cancel()

        self._state = new_state

        # Seed / wipe the linked-flashcards sub-VM. ``_sync_linked_flashcards`` is state-gated, so
        # we set ``_state`` first and let the sync helper do the right thing: in ``LINKED_FLASHCARDS``
        # it pushes the cursor entry id; back in ``ENTRIES`` we explicitly call ``set_entry_id(None)``
        # to invalidate any in-flight fetch and free the loaded window.
        if new_state is self.State.LINKED_FLASHCARDS:
            self._sync_linked_flashcards()
        else:
            self._linked_flashcards.set_entry_id(None)

        self.emit(self.dirty)

    def set_search(self, query: str) -> None:
        """Replace the active search query. Empty string clears the search."""
        new = query or ""
        if new == self._search:
            return
        self._search = new
        self._cursor = 0
        self._request_fetch()

    def set_sort(
        self,
        sort_by: EntrySortKey,
        sort_dir: Literal["asc", "desc"] = "asc",
    ) -> None:
        """Replace the active sort. Clears the selection (rows reshuffle and selection-by-position
        loses meaning), resets the cursor, and triggers a refetch from offset 0. The view computes
        whatever toggle/reset semantic it wants (header-click toggle, explicit reset, etc.) and calls
        this with concrete values."""
        if sort_by == self._sort_by and sort_dir == self._sort_dir:
            return
        self._sort_by = sort_by
        self._sort_dir = sort_dir
        self._cursor = 0
        self._clear_selection()
        self._request_fetch()

    def apply_filter(self, entry_types: tuple[EntryType, ...] | None) -> None:
        """Replace the active entry-type filter.

        ``None`` clears the filter. A tuple restricts the result set to those types. An empty tuple
        is a legal terminal state meaning "no rows match" (mirrors ``set_filter``'s topic-ids
        semantics).

        Idempotent against the current value. Clears the selection, resets the cursor, refetches.
        """
        new = None if entry_types is None else tuple(entry_types)
        if new == self._entry_types:
            return
        self._entry_types = new
        self._cursor = 0
        self._clear_selection()
        self._request_fetch()

    def apply_flashcard_filter(self, has_flashcards: bool | None) -> None:
        """Replace the active flashcard-presence filter.

        ``None`` clears the filter. ``True`` restricts to entries with at least one linked
        flashcard; ``False`` restricts to entries with none. Same window-reset semantics as
        ``apply_filter``: idempotent against the current value, otherwise clears the selection,
        resets the cursor, and refetches.
        """
        if has_flashcards == self._has_flashcards:
            return
        self._has_flashcards = has_flashcards
        self._cursor = 0
        self._clear_selection()
        self._request_fetch()

    def set_cursor(self, index: int) -> None:
        """Move the row cursor. Clamped to the loaded window. Pushes the new cursor's entry into
        ``self._details`` and emits ``dirty`` so the view repaints.

        The repaint includes a programmatic ``move_cursor`` on the rebuild path, which fires another
        ``DataTable.RowHighlighted`` and re-enters this method via ``on_data_table_row_highlighted``.
        That second call is a no-op thanks to the index-equality guard below — the bounce dies in one
        round-trip rather than looping.

        Note: the cursor is intentionally an index, not an entry id, because navigation is a window-local
        concern — after ``load_more`` extends the window, the same cursor position points at the same row.
        """
        if not self._entries:
            new = 0
        else:
            new = max(0, min(index, len(self._entries) - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self._sync_details()
        self._sync_linked_flashcards()
        self.emit(self.dirty)

    def toggle_multi_select(self) -> None:
        """Flip multi-select mode. Turning the mode **off** abandons the current selection (clears
        ``_selected_ids``); turning it on starts with an empty set. Pushes the resulting state into
        the details VM so the side panel can freeze its edits."""
        self._multi_select_active = not self._multi_select_active
        if not self._multi_select_active:
            self._selected_ids.clear()
        self._details.set_multi_select(
            self._multi_select_active,
            len(self._selected_ids),
        )
        self.emit(self.dirty)

    def toggle_current_selection(self) -> None:
        """Toggle membership of the cursor's entry in the selection set. No-op when multi-select is off or
        the window is empty — those are the cases where the action has no meaning."""
        if not self._multi_select_active or not self._entries:
            return
        entry_id = self._entries[self._cursor].id
        if entry_id in self._selected_ids:
            self._selected_ids.remove(entry_id)
        else:
            self._selected_ids.add(entry_id)
        self._details.set_multi_select(True, len(self._selected_ids))
        self.emit(self.dirty)

    def add_current_to_selection(self) -> None:
        """Idempotent add of the cursor's entry to the selection set — the half of
        ``toggle_current_selection`` that ``shift+up``/``shift+down`` uses for range-select. Held-key
        repeat across already-selected rows is a no-op, which is the right behaviour for sweeping the
        cursor through an extending range."""
        if not self._multi_select_active or not self._entries:
            return
        entry_id = self._entries[self._cursor].id
        if entry_id in self._selected_ids:
            return
        self._selected_ids.add(entry_id)
        self._details.set_multi_select(True, len(self._selected_ids))
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Bulk actions on the selection
    # ------------------------------------------------------------------
    #
    # "The selection" is whatever ``_selected_target_ids`` resolves: the explicit ``_selected_ids``
    # set in multi-select mode, the cursor's entry id in single-select mode. The three action methods
    # below all share that resolution and no-op against an empty result, so a stray invocation against
    # an empty window or empty selection is safe.

    async def delete_selected_entries(self) -> None:
        """Delete the resolved target entries from the DB and prune them from the loaded window.

        Each row is removed via ``delete_entry`` inside a single session + commit so partial-failure
        leaves an atomic DB state. The FK on ``flashcard_entry.entry_id`` cascades, so any flashcard-
        to-entry links pointing at deleted entries are cleaned up automatically; the flashcards
        themselves are unaffected.

        After the commit we update local state in place: filter ``self._entries``, decrement
        ``self._total``, clamp the cursor, clear ``self._selected_ids`` (multi-select), and reconcile
        ``_has_more``. No refetch — we know exactly which rows went away. In multi-select mode the
        mode stays on so the visual context is preserved.
        """
        targets = self._selected_target_ids()
        if not targets:
            return

        async with self._session_factory() as session:
            for entry_id in targets:
                await delete_entry(session, entry_id)
            await session.commit()
        _logger.info("Deleted %d entries", len(targets))

        self._entries = [e for e in self._entries if e.id not in targets]
        if self._total is not None:
            self._total = max(0, self._total - len(targets))
        if self._multi_select_active:
            self._selected_ids.clear()
        if self._cursor >= len(self._entries):
            self._cursor = max(0, len(self._entries) - 1)
        if self._total is not None:
            self._has_more = len(self._entries) < self._total

        self._sync_details()
        self._sync_linked_flashcards()
        self._details.set_multi_select(self._multi_select_active, 0)
        self.emit(self.dirty)

    async def change_topic_on_selected_entries(self, new_topic_id: int) -> None:
        """Reassign the topic of every target entry to ``new_topic_id``, then refetch.

        Refetch (rather than in-place mutation of cached rows) so any topic filter the user has active
        re-evaluates against the new values — an entry whose new topic is outside the filter should
        disappear from the window. Selection is preserved across the refetch only for entries that
        survive into the new window (``_selected_ids &= visible ids`` — see ``_post_change_refetch``).
        """
        targets = self._selected_target_ids()
        if not targets:
            return
        async with self._session_factory() as session:
            for entry_id in targets:
                await update_entry(session, entry_id, topic_id=new_topic_id)
            await session.commit()
        _logger.info("Re-topicked %d entries to topic %d", len(targets), new_topic_id)
        self._post_change_refetch()

    async def change_type_on_selected_entries(self, new_type: EntryType) -> None:
        """Reassign the type of every target entry, then refetch. Same selection-preservation rule as
        ``change_topic_on_selected_entries``."""
        targets = self._selected_target_ids()
        if not targets:
            return
        async with self._session_factory() as session:
            for entry_id in targets:
                await update_entry(session, entry_id, entry_type=new_type)
            await session.commit()
        _logger.info("Retyped %d entries to %s", len(targets), new_type.value)
        self._post_change_refetch()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_target_ids(self) -> set[int]:
        """Resolve "the selection" to a concrete set of entry ids. In multi-select mode that's
        ``_selected_ids``; in single-select mode it's the cursor's entry id (empty if the window is
        empty). Shared by all three bulk-action methods."""
        if self._multi_select_active:
            return set(self._selected_ids)
        if not self._entries:
            return set()
        return {self._entries[self._cursor].id}

    def _clear_selection(self) -> None:
        """Drop ``_selected_ids`` and push the new (zero) count to the details VM. No-op when the set
        is already empty. Used by mutators that reshuffle the window (sort / filter changes)."""
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        if self._multi_select_active:
            self._details.set_multi_select(True, 0)

    def _post_change_refetch(self) -> None:
        """Refetch and intersect ``_selected_ids`` against the new window. Fires through the normal
        debounced fetch path — the 50ms debounce is imperceptible after a modal action — and uses the
        ``on_complete`` callback so the selection intersection runs against the fresh window."""
        self._request_fetch(on_complete=self._intersect_selection_with_window)

    def _intersect_selection_with_window(self) -> None:
        """Drop any selected ids no longer visible in the loaded window. Fired as the ``on_complete``
        of a post-change refetch."""
        if not self._selected_ids:
            return
        visible = {e.id for e in self._entries}
        survived = self._selected_ids & visible
        if survived != self._selected_ids:
            self._selected_ids.intersection_update(visible)
            if self._multi_select_active:
                self._details.set_multi_select(True, len(self._selected_ids))

    def _sync_details(self) -> None:
        """Push the cursor's entry (or ``None``) into the detail sub-VM.

        Called whenever the cursor moves or the window is replaced. The sub-VM emits its own ``dirty``
        when the reference changes, so the detail view repaints independently of the tab view.
        """
        if not self._entries or self._cursor >= len(self._entries):
            self._details.set_entry(None)
            return
        self._details.set_entry(self._entries[self._cursor])

    def _sync_linked_flashcards(self) -> None:
        """Push the cursor's entry id (or ``None``) into the linked-flashcards sub-VM — but only
        while we're actually in ``State.LINKED_FLASHCARDS``. The sub-VM does no work when the
        tab isn't rendering it, so skipping the call in ``ENTRIES`` avoids spurious fetches.

        The transition into ``LINKED_FLASHCARDS`` calls this directly (``transition_to``) so the
        right-hand table seeds itself with the current cursor entry on entry. The transition out
        pushes ``None`` to wipe the window and invalidate any in-flight fetch.
        """
        if self._state is not self.State.LINKED_FLASHCARDS:
            return
        if not self._entries or self._cursor >= len(self._entries):
            self._linked_flashcards.set_entry_id(None)
            return
        self._linked_flashcards.set_entry_id(self._entries[self._cursor].id)

    def _on_details_saved(self) -> None:
        """Detail panel just persisted a buffered edit. The in-memory ``KnowledgeEntry`` at the cursor
        was mutated in place, so the cached row in this tab VM's ``self._entries`` already sees the new
        values — we just need to trigger a tab-view repaint so the ``DataTable`` row picks them up."""
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of entries to the current window.

        No-op if a fetch is already in flight (we don't want to race with a reset fetch) or if there's
        nothing more to load. Doesn't move the cursor.

        Doesn't go through ``_request_fetch`` — that resets the window from offset 0. We share
        ``_query_window`` with ``_fetch`` (same query, different offset), capture the current
        ``_fetch_id`` synchronously, and gate the append on ``_still_current`` so a reset operation
        landing mid-flight doesn't have us extend its new window with stale tail rows.
        """
        if self._is_loading or not self._has_more:
            return
        my_id = self._fetch_id
        kwargs = self._query_kwargs()
        more = await self._query_window(kwargs, offset=len(self._entries))
        if not self._still_current(my_id):
            return
        self._entries.extend(more)
        # If this page came back short, we know there's nothing further.
        if len(more) < self._limit:
            self._has_more = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # BrowserTabViewModel contract
    # ------------------------------------------------------------------

    def _query_kwargs(self) -> dict[str, Any]:
        """Snapshot the DB-query inputs into a plain dict. Captured synchronously at the call site
        (no await inside) so all queries derived from one snapshot see locally-consistent state, even
        if mutators run between the snapshot and the eventual query.

        Shared by ``_fetch`` (full reload) and ``load_more`` (append) so they always agree on what
        the "current query" is."""
        return {
            "topic_ids": self._filter_ids,
            "search": self._search or None,
            "entry_types": list(self._entry_types) if self._entry_types is not None else None,
            "has_flashcards": self._has_flashcards,
            "sort_by": self._sort_by,
            "sort_dir": self._sort_dir,
        }

    async def _query_window(
        self, kwargs: dict[str, Any], offset: int,
    ) -> list[KnowledgeEntry]:
        """Run the windowed SELECT at ``offset`` against a captured kwargs snapshot."""
        async with self._session_factory() as session:
            return await list_entries_paginated(
                session,
                limit=self._limit,
                offset=offset,
                **kwargs,
            )

    async def _fetch(self) -> tuple[list[KnowledgeEntry], int]:
        """Reload window + total against current filter/search/sort. Stateless: returns the data, lets
        ``_process_fetched_data`` apply it. Runs both queries (windowed SELECT and COUNT) before
        returning — the view paints once at the end, which is a tiny UX hit relative to the simplicity
        of the single-result contract."""
        kwargs = self._query_kwargs()
        rows = await self._query_window(kwargs, offset=0)
        async with self._session_factory() as session:
            total = await count_entries_filtered(
                session,
                topic_ids=kwargs["topic_ids"],
                search=kwargs["search"],
                entry_types=kwargs["entry_types"],
                has_flashcards=kwargs["has_flashcards"],
            )
        return rows, total

    def _process_fetched_data(
        self, result: tuple[list[KnowledgeEntry], int],
    ) -> None:
        """Apply a ``_fetch`` result to local state: replace the window, set the total, reconcile
        ``_has_more``, clamp the cursor, and re-point the detail / linked-flashcards sub-VMs at the
        (possibly different) entry under the cursor."""
        rows, total = result
        self._entries = rows
        self._total = total
        self._has_more = len(self._entries) < self._total
        if self._cursor >= len(self._entries):
            self._cursor = max(0, len(self._entries) - 1)
        self._sync_details()
        self._sync_linked_flashcards()
