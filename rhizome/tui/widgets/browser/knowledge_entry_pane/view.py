"""KnowledgeEntryBrowserPaneView — DataTable + details + status row.

See ``view_model.py`` for the VM contract and ``entry_details/`` for the
side panel.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from .entry_details import EntryDetailsView
from .view_model import KnowledgeEntryBrowserPaneViewModel


class _EntriesTable(DataTable):
    """``DataTable`` subclass that owns the multi-select keybindings.

    Lives here rather than as standalone bindings on the parent view so
    the keys only fire when the table is focused — ``m`` and ``space``
    on the details panel's ``TextArea``s would otherwise have to be
    suppressed. Both actions delegate straight to the pane VM; the
    table widget holds no state of its own.
    """

    BINDINGS = [
        Binding("m", "toggle_multi_select", show=False),
        Binding("space", "toggle_selection", show=False),
        # ``d`` only does something meaningful while multi-select is on
        # with a non-empty selection; the VM guards both, so we can fire
        # it unconditionally.
        Binding("d", "request_delete", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def action_toggle_multi_select(self) -> None:
        self._vm.toggle_multi_select()

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    def action_request_delete(self) -> None:
        self._vm.request_delete()


class _DeleteConfirm(Static, can_focus=True):
    """Bulk-delete confirmation dialog. Mirrors ``_ChoicesList`` from
    ``entry_details/view.py`` — a focusable ``Static`` with up/down/enter
    bindings dispatching to the VM, plus ``escape`` for quick dismissal.

    Renders three lines: a header explaining the action (entry count +
    the no-flashcards-harmed promise), then two indented choice rows
    (Confirm / Cancel). Cursor brightness tracks focus, same as
    ``_ChoicesList``.
    """

    BINDINGS = [
        Binding("up", "choice_up", show=False),
        Binding("down", "choice_down", show=False),
        Binding("enter", "choice_confirm", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus — re-render on focus changes for
        # the same reason ``_ChoicesList`` does.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        # Note: not ``_render`` — that's a Textual-internal name (the
        # widget's own ``_render`` returns the cached Visual). Naming
        # this method ``_render`` shadows the framework hook and
        # Textual tries to use the returned ``rich.text.Text`` as a
        # ``Visual``, blowing up in ``to_strips``.
        self.update(self._render_dialog())

    def _render_dialog(self) -> Text:
        count = len(self._vm.selected_ids)
        noun = "entry" if count == 1 else "entries"
        cursor_style = "bold" if self.has_focus else "#6a6a6a"
        text = Text()
        text.append(f"Delete {count} selected {noun}? ", style="bold")
        text.append(
            "Linked flashcards will not be affected.", style="dim",
        )
        text.append("\n")
        labels = ("Confirm", "Cancel")
        for i, label in enumerate(labels):
            chosen = i == self._vm.delete_choice_cursor
            if chosen:
                text.append("► ", style=cursor_style)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
            if i < len(labels) - 1:
                text.append("\n")
        return text

    def action_choice_up(self) -> None:
        self._vm.move_delete_cursor(-1)

    def action_choice_down(self) -> None:
        self._vm.move_delete_cursor(1)

    async def action_choice_confirm(self) -> None:
        if self._vm.delete_choice_cursor == 0:
            await self._vm.confirm_delete()
        else:
            self._vm.cancel_delete()

    def action_cancel(self) -> None:
        self._vm.cancel_delete()


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
    /* Multi-select wash: keep the zebra alternation but shift both rows
       darker, so the table reads as muted-but-structured and the bright-
       green selected rows pop. ``$surface-darken-2`` is the odd-row
       (table-base) colour; even rows sit one step above that, mirroring
       the regular-mode relative offset at a darker absolute level. */
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select {
        background: $surface-darken-2;
    }
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select > .datatable--even-row {
        background: $surface-darken-1 50%;
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
    KnowledgeEntryBrowserPaneView #delete-confirm {
        /* 3 lines of content (header + Confirm + Cancel) plus the
           ``border-top`` itself, which counts toward the box height. */
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm:focus {
        border-top: solid $accent;
    }
    """

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``delete_pending`` so ``_refresh`` can
        # detect the open / close transition and grab / restore focus.
        # Without this, opening the dialog wouldn't auto-focus it
        # (forcing the user to alt-tab around), and closing it would
        # leave focus on a ``display: none`` widget.
        self._was_delete_pending: bool = False
        # Signature of the entries list at the last refresh — a tuple
        # of entry ids in display order. Used by ``_refresh`` to decide
        # between a full ``clear()`` + rebuild (when row identity has
        # actually changed: refetch, delete, load_more) and a cheap
        # in-place ``update_cell_at`` pass (when only styles or markers
        # changed: mode toggle, selection toggle, post-edit content
        # mutation). The in-place path preserves ``DataTable``'s
        # scroll position and cursor — without it, every selection
        # toggle resets scroll to 0 and the auto-re-scroll lands the
        # cursor row at the bottom of the viewport instead of leaving
        # it where the user had it. ``None`` forces the first refresh
        # through the rebuild path (the table is empty then anyway).
        self._last_row_signature: tuple[int, ...] | None = None

    # Max display width for the title column. Anything longer is truncated
    # by ``DataTable`` (with an ellipsis). 50 is an arbitrary first-cut
    # tuned against the current sample data; lift if it ever bites.
    _TITLE_COLUMN_WIDTH = 50

    def compose(self):
        table = _EntriesTable(
            self._vm, id="entries-table", cursor_type="row", zebra_stripes=True,
        )
        # ``key`` strings give us a stable per-row id so cursor restoration
        # across reloads is possible later if we want it. They're not used
        # by the view today.
        # ``title`` is the only column with a fixed width — the rest
        # auto-size to their content. Without the cap, titles like the
        # 67-character "Linear Algebra: Vector Spaces …" expand the
        # column to the full width of the longest title, squeezing
        # everything else.
        #
        # The leading "sel" column is always present (we can't add or
        # drop columns cleanly after construction). When multi-select
        # is off the column renders empty; when on, each row shows
        # ``[ ]`` or ``[x]``. Width 3 fits the marker glyph; DataTable's
        # default cell padding takes care of the breathing room.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        with Horizontal(id="pane-body"):
            yield table
            yield EntryDetailsView(self._vm.details)
        yield _DeleteConfirm(self._vm, id="delete-confirm")
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
        mode = self._vm.multi_select_active
        # ``-multi-select`` triggers the CSS that darkens the zebra-row
        # palette while the user is picking.
        table.set_class(mode, "-multi-select")

        # Decide between a full rebuild and in-place cell updates by
        # comparing the row identity. Anything that actually shuffles
        # the entries list (refetch, bulk-delete, load_more) gets a
        # ``clear()`` + ``add_row`` pass; pure style/marker changes
        # (mode toggle, selection toggle, post-edit content mutation)
        # ride the ``update_cell_at`` path and inherit ``DataTable``'s
        # existing scroll + cursor state.
        new_signature = tuple(e.id for e in self._vm.entries)
        rebuild = new_signature != self._last_row_signature
        if rebuild:
            table.clear()
        for i, entry in enumerate(self._vm.entries):
            type_str = entry.entry_type.value if entry.entry_type is not None else "—"
            # Three colouring regimes:
            #   * not multi-select: zebra-pair text (odd rows dim) so the
            #     stripe background shows through evenly.
            #   * multi-select, not selected: same zebra-pair pattern but
            #     with both colours shifted darker — so the whole table
            #     reads as muted-but-structured.
            #   * multi-select, selected: bright green + bold to pop
            #     against the dimmed sea around them.
            selected = mode and entry.id in self._vm.selected_ids
            if selected:
                style = "bold #5fd75f"
            elif mode:
                style = "#787878" if i % 2 else "#a0a0a0"
            else:
                style = "#a0a0a0" if i % 2 else ""
            marker = ("[x]" if selected else "[ ]") if mode else ""
            cells = (
                Text(marker, style=style),
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                Text(str(entry.topic_id), style=style),
            )
            if rebuild:
                table.add_row(*cells, key=str(entry.id))
            else:
                # In-place: overwrite each cell in row ``i``. Style is
                # carried inside each ``Text`` value so this picks up
                # the new colours/bold for free.
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
        self._last_row_signature = new_signature

        # After a rebuild, ``table.clear()`` reset the table cursor to
        # row 0. Push the VM's cursor back into the table so the
        # highlight lands on the row the VM expects. ``move_cursor``
        # fires ``RowHighlighted``, which round-trips into
        # ``vm.set_cursor`` — the early-return-on-equality there keeps
        # this from looping. On the in-place path the cursor was never
        # disturbed, so we skip this entirely.
        if (
            rebuild
            and self._vm.entries
            and 0 <= self._vm.cursor < len(self._vm.entries)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#pane-status", Static)
        status.update(self._format_status())

        # Delete-confirm dialog: visibility + focus rescue. Mirrors the
        # ``_was_dirty`` pattern in ``EntryDetailsView`` — we need the
        # previous state to detect the open/close edges. On open, grab
        # focus to the dialog so the user can press enter immediately;
        # on close, return focus to the table so they're not stranded
        # on a ``display: none`` widget. The repeat-while-pending branch
        # is a no-op.
        dialog = self.query_one("#delete-confirm", _DeleteConfirm)
        pending = self._vm.delete_pending
        dialog.set_class(pending, "-visible")
        if pending and not self._was_delete_pending:
            dialog.focus()
        elif self._was_delete_pending and not pending:
            try:
                self.query_one("#entries-table", DataTable).focus()
            except Exception:
                # Table may have been unmounted (e.g. pane swap mid-close);
                # nothing useful to do, just let the focus settle wherever.
                pass
        self._was_delete_pending = pending

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
        tree. Land on the leftmost focusable sub-region — normally the
        table, but if the delete-confirm dialog is open we re-focus it
        instead so the user picks up where they left off after a tree
        side-trip (alt+left from the dialog hops back to the tree)."""
        if self._vm.delete_pending:
            try:
                self.query_one("#delete-confirm", _DeleteConfirm).focus()
                return
            except Exception:
                # Fall through to the normal landing if the dialog
                # isn't mounted yet.
                pass
        self.query_one("#entries-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        if focused is table:
            # While multi-select is on, the details panel is frozen and
            # has no useful edit affordances — short-circuit the
            # transition so ``alt+right`` keeps the user on the table.
            # Returning False here lets ``BrowserView.action_focus_right``
            # treat the table as the rightmost edge.
            if self._vm.multi_select_active:
                return False
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
        if self._vm.multi_select_active:
            # Multi-select takes over the status line — the "N of M"
            # hint is still useful but secondary, so we lead with the
            # selection count.
            count = len(self._vm.selected_ids)
            noun = "entry" if count == 1 else "entries"
            return f"multi-select: {count} {noun} selected (m to exit, space to toggle)"
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
