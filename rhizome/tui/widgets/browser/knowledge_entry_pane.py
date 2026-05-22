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

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from rhizome.db import KnowledgeEntry
from rhizome.db.operations import (
    EntrySortKey,
    count_entries_filtered,
    list_entries_paginated,
)
from rhizome.logs import get_logger

from .entry_details import EntryDetailsView, EntryDetailsViewModel
from .pane_base import BrowserPaneViewModel

_logger = get_logger("browser.knowledge_entry_pane")

# Hard cap on the rows fetched per page. See braindump for the rationale: at
# 100K+ entries we want a bounded memory + render footprint, and "showing 500
# of N+, load more" is the simplest UX that scales. Lifting this is a one-line
# change; switching to keyset pagination is the longer-term migration.
DEFAULT_PAGE_LIMIT = 500


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
        # active — the DB op treats falsy strings as "no filter".
        self._search: str = ""
        self._sort_by: EntrySortKey = "created_at"
        self._sort_dir: Literal["asc", "desc"] = "asc"

        # Row cursor within the currently-loaded window. The view owns
        # navigation; the VM owns the persisted position so it survives
        # repaints. Reset to 0 on any "reset" operation.
        self._cursor: int = 0

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


class KnowledgeEntryBrowserPaneView(Vertical):
    """Minimal view for ``KnowledgeEntryBrowserPaneViewModel``: a DataTable
    plus a one-line status row beneath. No detail panel, no search bar —
    those are explicitly out of scope for the first cut (see the braindump
    and the agreed iteration plan).

    Columns: id / title / type / topic_id. Title is truncated at render time
    (column width is bounded by the DataTable's auto-layout). Type renders
    as the enum value string, or ``—`` for entries with no type set.
    """

    DEFAULT_CSS = """
    KnowledgeEntryBrowserPaneView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    KnowledgeEntryBrowserPaneView #pane-body {
        layout: horizontal;
        height: 1fr;
    }
    KnowledgeEntryBrowserPaneView #entries-table {
        width: 60%;
        height: 1fr;
    }
    KnowledgeEntryBrowserPaneView EntryDetailsView {
        width: 40%;
        height: 1fr;
    }
    KnowledgeEntryBrowserPaneView #pane-status {
        dock: bottom;
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    # Max display width for the title column. Anything longer is truncated
    # by ``DataTable`` (with an ellipsis). 50 is an arbitrary first-cut
    # tuned against the current sample data; lift if it ever bites.
    _TITLE_COLUMN_WIDTH = 50

    def compose(self):
        table = DataTable(id="entries-table", cursor_type="row", zebra_stripes=True)
        # ``key`` strings give us a stable per-row id so cursor restoration
        # across reloads is possible later if we want it. They're not used
        # by the view today.
        # ``title`` is the only column with a fixed width — the rest
        # auto-size to their content. Without the cap, titles like the
        # 67-character "Linear Algebra: Vector Spaces …" expand the
        # column to the full width of the longest title, squeezing
        # everything else.
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        with Horizontal(id="pane-body"):
            yield table
            yield EntryDetailsView(self._vm.details)
        yield Static("", id="pane-status")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # If the VM already has data (it was bootstrapped before the view
        # mounted), paint it on first frame instead of waiting for the next
        # dirty.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        # Full rebuild on every refresh. With the LIMIT-500 cap this is
        # always cheap; if we move to virtualized scrolling we'll switch to
        # delta updates here.
        table.clear()
        for i, entry in enumerate(self._vm.entries):
            type_str = entry.entry_type.value if entry.entry_type is not None else "—"
            # Alternate text style every other row to pair with zebra_stripes'
            # background alternation — odd rows render dim.
            style = "#a0a0a0" if i % 2 else ""
            table.add_row(
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                Text(str(entry.topic_id), style=style),
                key=str(entry.id),
            )

        # After ``table.clear()`` the table cursor resets to row 0. Push
        # the VM's cursor back into the table so the highlight lands on
        # the row the VM expects. ``move_cursor`` fires ``RowHighlighted``,
        # which round-trips into ``vm.set_cursor`` — the early-return-on-
        # equality there keeps this from looping.
        if self._vm.entries and 0 <= self._vm.cursor < len(self._vm.entries):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#pane-status", Static)
        status.update(self._format_status())

    # ------------------------------------------------------------------
    # Cross-region focus (driven by ``BrowserView``'s alt+left/right)
    # ------------------------------------------------------------------
    #
    # Two regions at this level: the entries table and the details
    # panel. The details panel has its own internal cycle (title →
    # content → choices) which we delegate to ``EntryDetailsView``. The
    # bool returns let the ``BrowserView`` know when the pane is at its
    # leftmost edge so it can roll focus back to the tree.

    def focus_first(self) -> None:
        """Entry point when ``BrowserView`` enters the pane from the
        tree. Land on the leftmost focusable sub-region: the table."""
        self.query_one("#entries-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        if focused is table:
            details.focus_first()
            return True
        if focused is not None and details in focused.ancestors_with_self:
            return details.focus_next_region()
        # Defensive fallback: focus was somewhere unexpected inside the
        # pane. Start the cycle from the leftmost region.
        self.focus_first()
        return True

    def focus_prev_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        if focused is table:
            # Pane's leftmost edge — let ``BrowserView`` hand focus to
            # the tree.
            return False
        if focused is not None and details in focused.ancestors_with_self:
            moved = details.focus_prev_region()
            if not moved:
                table.focus()
            return True
        return False

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Table cursor moved — push the row index into the VM.

        The VM's ``set_cursor`` no-ops if the index is unchanged, so this
        is safe to fire from programmatic ``move_cursor`` calls during
        ``_refresh`` (and from the initial mount, where the table seeds
        its cursor to row 0).
        """
        if event.data_table.id != "entries-table":
            return
        self._vm.set_cursor(event.cursor_row)

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        total = self._vm.total
        loaded = len(self._vm.entries)
        if total is None:
            # Window fetched but count not yet in — happens briefly between
            # the two queries in ``_fetch``.
            if loaded == 0:
                return "no entries"
            return f"{loaded} loaded"
        if loaded < total:
            return f"showing {loaded} of {total}"
        if total == 0:
            return "no entries"
        if total == 1:
            return "1 entry"
        return f"{total} entries"
