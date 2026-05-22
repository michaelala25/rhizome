"""KnowledgeEntryBrowserPaneView — DataTable + details + status row.

See ``view_model.py`` for the VM contract and ``entry_details/`` for the
side panel.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from .entry_details import EntryDetailsView
from .view_model import KnowledgeEntryBrowserPaneViewModel


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
