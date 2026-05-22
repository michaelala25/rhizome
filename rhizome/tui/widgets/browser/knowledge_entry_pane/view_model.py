"""KnowledgeEntryBrowserPaneViewModel — the first concrete browser pane.

Shows ``KnowledgeEntry`` rows matching the orchestrator's topic filter (plus
its own search/sort state) in a fixed-size window. Total counts and
pagination are kept deliberately simple for the MVP: a single LIMIT-N window
with a "showing N of M" hint, and an explicit ``load_more`` for the next
page. Once we want true virtualized scroll, the seam is at ``_fetch`` — swap
the offset-based call for a keyset-paginated one and the rest of the VM
keeps working.

Filter, search, and sort are all "reset" operations: changing any of them
discards the current window and refetches from offset 0, resetting the row
cursor. ``load_more`` is an "append" operation — it extends the existing
window without touching the cursor.
"""

from __future__ import annotations

from typing import Any, Literal

from rhizome.db import KnowledgeEntry
from rhizome.db.operations import (
    EntrySortKey,
    count_entries_filtered,
    delete_entry,
    list_entries_paginated,
)
from rhizome.logs import get_logger

from ..pane_base import BrowserPaneViewModel
from .entry_details import EntryDetailsViewModel

_logger = get_logger("browser.knowledge_entry_pane")

# Hard cap on the rows fetched per page. See braindump for the rationale: at
# 100K+ entries we want a bounded memory + render footprint, and "showing 500
# of N+, load more" is the simplest UX that scales. Lifting this is a one-line
# change; switching to keyset pagination is the longer-term migration.
DEFAULT_PAGE_LIMIT = 500

# The sort axes the UI lets the user pick from. Ordered left-to-right
# the way they're laid out in the sort dialog (which mirrors the data
# table's column order). The DB op accepts a wider set (``created_at`` /
# ``updated_at``) for backward compat; the dialog deliberately surfaces
# only the four most useful axes.
SORT_OPTIONS: tuple[EntrySortKey, ...] = ("id", "title", "type", "topic")


class KnowledgeEntryBrowserPaneViewModel(BrowserPaneViewModel):
    """Concrete pane VM for browsing knowledge entries."""

    TITLE = "Knowledge Entries"

    def __init__(
        self,
        session_factory: Any,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        super().__init__(session_factory)
        self._limit = limit

        # Result window state. ``_entries`` is the currently-loaded rows;
        # ``_total`` is the count of rows matching the filter (None until the
        # first count-query lands). ``_has_more`` is true when the loaded
        # window doesn't cover the full result set.
        self._entries: list[KnowledgeEntry] = []
        self._total: int | None = None
        self._has_more: bool = False

        # Search/sort state. ``_search`` is an empty string when no search is
        # active — the DB op treats falsy strings as "no filter". Default
        # sort is ``id`` (matches the UI's default position in the sort
        # dialog and gives the user a stable, predictable initial view).
        self._search: str = ""
        self._sort_by: EntrySortKey = "id"
        self._sort_dir: Literal["asc", "desc"] = "asc"

        # Row cursor within the currently-loaded window. The view owns
        # navigation; the VM owns the persisted position so it survives
        # repaints. Reset to 0 on any "reset" operation.
        self._cursor: int = 0

        # Multi-select state. When ``_multi_select_active`` is True the
        # view paints a leading marker column ("[x]"/"[ ]") and the user
        # can toggle selection of the cursor's row with ``space``.
        # ``_selected_ids`` is keyed by entry id (not row index) so the
        # selection survives ``load_more`` and refetches. Turning the
        # mode off clears the set ("abandons the selection").
        self._multi_select_active: bool = False
        self._selected_ids: set[int] = set()

        # Pending bulk-delete confirmation. Flipped on by ``request_delete``
        # (the user pressed ``d`` with a non-empty selection); the view
        # reveals a confirm dialog whose Confirm/Cancel cursor lives in
        # ``_delete_choice_cursor`` (0 = Confirm, 1 = Cancel). ``confirm_delete``
        # / ``cancel_delete`` are the only exits.
        self._delete_pending: bool = False
        self._delete_choice_cursor: int = 0

        # Pending sort dialog. ``_sort_cursor`` indexes into ``SORT_OPTIONS``
        # — initialized lazily by ``request_sort`` so it lands on the
        # currently-active sort key.
        self._sort_pending: bool = False
        self._sort_cursor: int = 0

        # The detail panel's VM. We push it the cursor's entry via
        # ``_sync_details`` whenever the cursor moves or the window
        # reloads. The pane view picks the VM up via ``self.details`` to
        # construct its companion ``EntryDetailsView``. We subscribe to
        # its ``SAVED`` callback so that after an Accept we can repaint
        # the table row (the in-memory ``KnowledgeEntry`` was mutated in
        # place, but the ``DataTable`` doesn't know that until we emit
        # ``dirty`` here).
        self._details = EntryDetailsViewModel(session_factory)
        self._details.subscribe(self._details.saved, self._on_details_saved)

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

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
    def cursor(self) -> int:
        return self._cursor

    @property
    def details(self) -> EntryDetailsViewModel:
        """Sub-VM driving the entry detail panel. Owned by this pane VM;
        the view picks it up to construct the companion view."""
        return self._details

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def selected_ids(self) -> set[int]:
        """Live reference to the selected-id set. Callers must not
        mutate it — use ``toggle_current_selection`` /
        ``toggle_multi_select`` instead. Matches the trust convention
        used by ``entries`` (also returned by reference)."""
        return self._selected_ids

    def is_selected(self, entry_id: int) -> bool:
        return entry_id in self._selected_ids

    @property
    def delete_pending(self) -> bool:
        return self._delete_pending

    @property
    def delete_choice_cursor(self) -> int:
        return self._delete_choice_cursor

    @property
    def sort_pending(self) -> bool:
        return self._sort_pending

    @property
    def sort_cursor(self) -> int:
        return self._sort_cursor

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

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
        """Replace the active sort. Triggers a refetch from offset 0."""
        if sort_by == self._sort_by and sort_dir == self._sort_dir:
            return
        self._sort_by = sort_by
        self._sort_dir = sort_dir
        self._cursor = 0
        self._request_fetch()

    def set_cursor(self, index: int) -> None:
        """Move the row cursor. Clamped to the loaded window.

        Pushes the new cursor's entry into ``self._details``; does **not**
        emit ``dirty`` itself, because the pane view's ``_refresh`` does a
        full table rebuild and rebuilding while the cursor is mid-move
        causes a feedback loop with ``DataTable``'s ``RowHighlighted``
        event. Cursor moves are visible via the ``DataTable``'s own
        cursor rendering and via the detail panel's dirty.

        Note: the cursor is intentionally an index, not an entry id, because
        navigation is a window-local concern — after ``load_more`` extends the
        window, the same cursor position points at the same row.
        """
        if not self._entries:
            new = 0
        else:
            new = max(0, min(index, len(self._entries) - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self._sync_details()

    def toggle_multi_select(self) -> None:
        """Flip multi-select mode. Turning the mode **off** abandons the
        current selection (clears ``_selected_ids``) and dismisses any
        open delete-confirm dialog (it'd be operating on a now-empty set);
        turning it on starts with an empty set. Pushes the resulting
        state into the details VM so the side panel can freeze its
        edits."""
        self._multi_select_active = not self._multi_select_active
        if not self._multi_select_active:
            self._selected_ids.clear()
            self._delete_pending = False
            self._delete_choice_cursor = 0
        self._details.set_multi_select(
            self._multi_select_active,
            len(self._selected_ids),
        )
        self.emit(self.dirty)

    def toggle_current_selection(self) -> None:
        """Toggle membership of the cursor's entry in the selection set.
        No-op when multi-select is off or the window is empty — those
        are the cases where the action has no meaning."""
        if not self._multi_select_active or not self._entries:
            return
        entry_id = self._entries[self._cursor].id
        if entry_id in self._selected_ids:
            self._selected_ids.remove(entry_id)
        else:
            self._selected_ids.add(entry_id)
        self._details.set_multi_select(True, len(self._selected_ids))
        self.emit(self.dirty)

    def request_delete(self) -> None:
        """Open the bulk-delete confirmation. No-op unless multi-select is
        active and the selection set is non-empty — pressing ``d`` with
        nothing selected (or outside multi-select) is meaningless. Idempotent
        when the dialog is already open."""
        if not self._multi_select_active or not self._selected_ids:
            return
        if self._delete_pending:
            return
        self._delete_pending = True
        self._delete_choice_cursor = 0
        self.emit(self.dirty)

    def move_delete_cursor(self, direction: int) -> None:
        """Move the Confirm/Cancel cursor in the dialog. Mod-2 wrap; no-op
        when the dialog isn't open."""
        if not self._delete_pending:
            return
        new = (self._delete_choice_cursor + direction) % 2
        if new == self._delete_choice_cursor:
            return
        self._delete_choice_cursor = new
        self.emit(self.dirty)

    def cancel_delete(self) -> None:
        """Dismiss the dialog without deleting anything. Multi-select state
        and the selection set are untouched."""
        if not self._delete_pending:
            return
        self._delete_pending = False
        self._delete_choice_cursor = 0
        self.emit(self.dirty)

    async def confirm_delete(self) -> None:
        """Bulk-delete the currently-selected entries from the DB, prune
        them from the loaded window, and dismiss the dialog.

        Each ``KnowledgeEntry`` row is removed via ``delete_entry`` inside
        a single session + commit so partial-failure leaves an atomic
        DB state. The FK on ``flashcard_entry.entry_id`` cascades, so
        any flashcard-to-entry links pointing at deleted entries are
        cleaned up automatically; the flashcards themselves are
        unaffected (which is what the dialog promises the user).

        After the commit we update local state in place: filter
        ``self._entries``, decrement ``self._total``, clamp the cursor,
        clear ``self._selected_ids``, and reconcile ``_has_more``. No
        refetch — we know exactly which rows went away.

        Multi-select mode stays on so the visual context is preserved;
        the user can hit ``m`` to exit when they're done.
        """
        if not self._delete_pending or not self._selected_ids:
            # Defensive: the dialog shouldn't be visible without a non-empty
            # selection, but a stray callback could still fire here.
            self._delete_pending = False
            self.emit(self.dirty)
            return

        to_delete = set(self._selected_ids)
        async with self._session_factory() as session:
            for entry_id in to_delete:
                await delete_entry(session, entry_id)
            await session.commit()
        _logger.info("Bulk deleted %d entries", len(to_delete))

        # Prune local state. Filter the window in-place to preserve order.
        self._entries = [e for e in self._entries if e.id not in to_delete]
        if self._total is not None:
            self._total = max(0, self._total - len(to_delete))
        self._selected_ids.clear()
        if self._cursor >= len(self._entries):
            self._cursor = max(0, len(self._entries) - 1)
        if self._total is not None:
            self._has_more = len(self._entries) < self._total

        self._delete_pending = False
        self._delete_choice_cursor = 0

        # Re-point the detail panel and tell it the new (zero) selection
        # count, then emit one dirty for the table repaint.
        self._sync_details()
        self._details.set_multi_select(self._multi_select_active, 0)
        self.emit(self.dirty)

    def request_sort(self) -> None:
        """Open the sort dialog. Sort takes priority over delete — any
        pending delete-confirm is dismissed first so the user doesn't end
        up with both dialogs jockeying for the same screen slot. The
        cursor lands on the currently-active sort axis so the most common
        action (toggle direction of the active sort) is a single ``enter``
        away. Idempotent when the dialog is already open."""
        if self._sort_pending:
            return
        self._delete_pending = False
        self._delete_choice_cursor = 0
        self._sort_pending = True
        try:
            self._sort_cursor = SORT_OPTIONS.index(self._sort_by)
        except ValueError:
            # Active sort isn't in the UI's surfaced set (e.g. legacy
            # ``created_at`` from an older session). Park on ``id``.
            self._sort_cursor = 0
        self.emit(self.dirty)

    def cancel_sort(self) -> None:
        """Dismiss the sort dialog without applying anything."""
        if not self._sort_pending:
            return
        self._sort_pending = False
        self.emit(self.dirty)

    def move_sort_cursor(self, direction: int) -> None:
        """Move the sort-axis cursor left (-1) or right (+1) with wrap.
        No-op when the dialog isn't open."""
        if not self._sort_pending:
            return
        new = (self._sort_cursor + direction) % len(SORT_OPTIONS)
        if new == self._sort_cursor:
            return
        self._sort_cursor = new
        self.emit(self.dirty)

    def apply_sort(self) -> None:
        """Confirm the highlighted axis. If it matches the current sort,
        flip the direction; otherwise switch to that axis in ascending
        order. Either way, **clears any active selection** — the
        ``LIMIT 500`` window is reshuffled by a refetch, and tracking
        selections across windows that don't necessarily include the
        same rows is more trouble than it's worth (see the dialog hint).
        Closes the dialog regardless of outcome."""
        if not self._sort_pending:
            return
        chosen = SORT_OPTIONS[self._sort_cursor]
        if chosen == self._sort_by:
            new_dir: Literal["asc", "desc"] = (
                "desc" if self._sort_dir == "asc" else "asc"
            )
        else:
            new_dir = "asc"
        self._sort_pending = False

        # Drop selections before triggering the refetch. We do this even
        # when the chosen sort matches the current one (direction-flip)
        # because the row order — and thus what the user "selected by
        # position" — has changed.
        if self._selected_ids:
            self._selected_ids.clear()
            if self._multi_select_active:
                self._details.set_multi_select(True, 0)

        # ``set_sort`` short-circuits when nothing changed, so direction
        # toggles still go through the refetch path.
        self.set_sort(chosen, new_dir)

    def _sync_details(self) -> None:
        """Push the cursor's entry (or ``None``) into the detail sub-VM.

        Called whenever the cursor moves or the window is replaced. The
        sub-VM emits its own ``dirty`` when the reference changes, so the
        detail view repaints independently of the pane view.
        """
        if not self._entries or self._cursor >= len(self._entries):
            self._details.set_entry(None)
            return
        self._details.set_entry(self._entries[self._cursor])

    def _on_details_saved(self) -> None:
        """Detail panel just persisted a buffered edit. The in-memory
        ``KnowledgeEntry`` at the cursor was mutated in place, so the
        cached row in this pane VM's ``self._entries`` already sees the
        new values — we just need to trigger a pane-view repaint so the
        ``DataTable`` row picks them up."""
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of entries to the current window.

        No-op if a fetch is already in flight (we don't want to race with a
        reset fetch) or if there's nothing more to load. Doesn't move the
        cursor.
        """
        if self._is_loading or not self._has_more:
            return
        # We deliberately do NOT go through ``_request_fetch`` here — that
        # cancels and resets, which would lose the appended rows. Instead we
        # do the fetch inline and mutate ``_entries`` directly. If a "reset"
        # operation lands while this is mid-flight, ``set_filter`` /
        # ``set_search`` / ``set_sort`` will overwrite ``_entries`` and the
        # appended rows from this call become harmless dead writes — they
        # never get emitted because the dirty after assignment lost the race
        # with the reset's dirty. (Mild waste of a query; acceptable for the
        # MVP. A future revision could track an append-task identity the same
        # way ``_run_fetch`` does.)
        async with self._session_factory() as session:
            more = await list_entries_paginated(
                session,
                topic_ids=self._filter_ids,
                search=self._search or None,
                sort_by=self._sort_by,
                sort_dir=self._sort_dir,
                limit=self._limit,
                offset=len(self._entries),
            )
        self._entries.extend(more)
        # If this page came back short, we know there's nothing further.
        if len(more) < self._limit:
            self._has_more = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # BrowserPaneViewModel contract
    # ------------------------------------------------------------------

    async def _fetch(self) -> None:
        """Reload the window + total against current filter/search/sort.

        Runs two queries: the windowed SELECT first (so the view can paint
        rows as soon as possible) followed by the COUNT for the "N of M"
        hint. Each query uses its own session so we don't pin a connection
        across both; both share the same cancellation point at the await.
        """
        # Reset the total to "not yet known" — keeps the hint honest while we
        # refetch, instead of showing the stale value from the previous
        # filter.
        self._total = None
        self._has_more = False

        async with self._session_factory() as session:
            self._entries = await list_entries_paginated(
                session,
                topic_ids=self._filter_ids,
                search=self._search or None,
                sort_by=self._sort_by,
                sort_dir=self._sort_dir,
                limit=self._limit,
                offset=0,
            )
        # Conservative initial estimate; the COUNT below either confirms or
        # corrects it. If we hit the limit exactly, there *might* be more.
        self._has_more = len(self._entries) >= self._limit
        # Clamp the cursor to the new window (it may have shrunk).
        if self._cursor >= len(self._entries):
            self._cursor = max(0, len(self._entries) - 1)
        # Re-point the detail panel at the (possibly different) entry now
        # under the cursor. Done before the dirty emit so the table
        # rebuild and the detail repaint happen in the same Textual frame.
        self._sync_details()
        self.emit(self.dirty)

        async with self._session_factory() as session:
            self._total = await count_entries_filtered(
                session,
                topic_ids=self._filter_ids,
                search=self._search or None,
            )
        # Reconcile has_more against the authoritative count.
        self._has_more = len(self._entries) < self._total
        # Don't emit dirty here — the base class's _run_fetch finally clause
        # emits one final dirty after _fetch returns, which covers this.
