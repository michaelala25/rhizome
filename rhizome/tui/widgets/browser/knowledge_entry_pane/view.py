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
from .view_model import (
    SORT_OPTIONS,
    KnowledgeEntryBrowserPaneViewModel,
    MultiSelectFilterViewModel,
)


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
        # ``s`` opens the sort dialog. Available in both regular and
        # multi-select mode (the VM clears any selection when the sort
        # is applied — see the dialog warning).
        Binding("s", "request_sort", show=False),
        # ``f`` opens the filter dialog. Mutually exclusive with sort /
        # delete (the VM cancels whichever is open when ``f`` lands).
        Binding("f", "request_filter", show=False),
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

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()


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
        # Sort takes priority over delete — pressing ``s`` from inside
        # the delete dialog dismisses it and opens the sort bar in one
        # step. ``vm.request_sort`` is the path that enforces the
        # priority (it cancels any pending delete before opening sort).
        Binding("s", "request_sort", show=False),
        # Same shape for the filter dialog.
        Binding("f", "request_filter", show=False),
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

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()


class _SortBar(Static, can_focus=True):
    """Sort-axis picker dialog. Sits in the same screen slot as
    ``_DeleteConfirm`` — only one is ever visible at a time, and the
    VM enforces priority (``request_sort`` cancels any pending delete).

    Renders horizontally, mirroring the data table's column order:
    ``id   title   type   topic``. The active sort is decorated with
    an arrow (``↑`` / ``↓``) and brackets; the cursor option is shown
    in a bold accent colour (no ``►`` prefix — keeping the row at a
    fixed width avoids the option labels jumping around as the cursor
    moves). A second line carries a help hint, extended with a
    selection-clearing warning while multi-select is on.

    Keys: ``left`` / ``right`` move the cursor (with wrap); ``enter``
    applies (toggles direction when on the active axis, otherwise
    switches to that axis ascending); ``s`` and ``escape`` dismiss
    without applying.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "apply", show=False),
        # ``r`` resets to the default sort (``id`` ascending). Mirrors
        # the same key in the filter dialog.
        Binding("r", "reset", show=False),
        # ``s`` toggles the dialog closed — symmetric with the
        # ``s``-opens-it binding on ``_EntriesTable``.
        Binding("s", "cancel", show=False),
        # ``f`` swaps to the filter dialog. ``request_filter`` on the
        # VM dismisses sort first so the two never co-exist.
        Binding("f", "request_filter", show=False),
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
        # Cursor colour brightens on focus, same convention as the
        # ``_ChoicesList`` / ``_DeleteConfirm`` widgets.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        active_idx = (
            SORT_OPTIONS.index(self._vm.sort_by)
            if self._vm.sort_by in SORT_OPTIONS
            else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Cursor colour: bright gold on focus, dim grey otherwise. The
        # active axis itself always renders in the default fg so the
        # arrow + brackets carry the "this is the live sort" signal.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(SORT_OPTIONS):
            is_active = i == active_idx
            is_cursor = i == self._vm.sort_cursor
            label = f"{arrow}[{option}]" if is_active else option
            if is_cursor:
                style = cursor_color
            elif is_active:
                style = ""  # default fg
            else:
                style = "#787878"
            text.append(label, style=style)
            if i < len(SORT_OPTIONS) - 1:
                text.append("   ")
        text.append("\n")

        # Help line — extended with the selection-clearing warning when
        # in multi-select. The warning sits inline rather than on a third
        # line so the dialog can stay at a fixed 4-line height across
        # both modes.
        hint = Text()
        hint.append(
            "← / → move • enter apply • r reset • s/esc dismiss", style="dim",
        )
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Applying clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def action_cursor_left(self) -> None:
        self._vm.move_sort_cursor(-1)

    def action_cursor_right(self) -> None:
        self._vm.move_sort_cursor(1)

    def action_apply(self) -> None:
        self._vm.apply_sort()

    def action_reset(self) -> None:
        self._vm.reset_sort()

    def action_cancel(self) -> None:
        self._vm.cancel_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()


class _FilterDialog(Static, can_focus=True):
    """Per-axis filter picker. Shares the same screen slot as
    ``_SortBar`` and ``_DeleteConfirm`` (the three are mutually
    exclusive at the VM level).

    The widget is built to accept multiple filter "categories" — each a
    ``FilterCategoryViewModel`` subclass — even though the pane currently
    only carries one (type). The top line shows the category tabs;
    underneath sits whatever input shape the active category needs.
    Rendering and key handling dispatch on the concrete category type
    (currently just ``MultiSelectFilterViewModel``): adding a new
    category type means a new subclass plus one new branch in both
    ``_render_active_category`` and the keystroke handlers.

    Keys:
      * ``tab`` / ``shift+tab`` — cycle categories (no-op with one)
      * ``left`` / ``right`` — move the cursor within the active category
      * ``space`` — toggle the cursor's option (MultiSelect)
      * ``r`` — reset every category to default
      * ``s`` — swap to the sort dialog
      * ``f`` / ``escape`` — dismiss
    """

    BINDINGS = [
        Binding("tab", "cycle_category(1)", show=False),
        Binding("shift+tab", "cycle_category(-1)", show=False),
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("space", "toggle", show=False),
        Binding("r", "reset", show=False),
        Binding("s", "request_sort", show=False),
        # ``f`` toggles the dialog closed (symmetric with the
        # ``f``-opens-it binding on ``_EntriesTable``).
        Binding("f", "cancel", show=False),
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
        # Cursor colours brighten on focus, matching the other dialogs.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_dialog())

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_dialog(self) -> Text:
        categories = self._vm.filter_categories
        active = self._vm.filter_active_category

        text = Text()
        # Line 1 — category tabs. The active tab gets brackets; non-
        # default categories pick up a green tint so the user can see
        # at a glance which filters are currently narrowing the view.
        text.append("filter by:  ", style="dim")
        for i, cat in enumerate(categories):
            is_active = active is cat
            colour = "#5fd75f" if not cat.is_default else ""
            if is_active:
                text.append("[", style=colour or "")
                text.append(cat.name, style=("bold " + colour).strip())
                text.append("]", style=colour or "")
            else:
                text.append(cat.name, style=colour or "#787878")
            if i < len(categories) - 1:
                text.append("   ")
        text.append("\n")

        # Line 2 — active category body. Dispatch on category type.
        if active is not None:
            text.append_text(self._render_active_category(active))
        text.append("\n")

        # Line 3 — hint, extended with the selection-clearing warning
        # while multi-select is on (mirrors ``_SortBar``'s pattern).
        hint = Text()
        bits = []
        if len(categories) > 1:
            bits.append("tab switch")
        bits.append("← / → move")
        if isinstance(active, MultiSelectFilterViewModel):
            bits.append("space toggle")
        bits.append("r reset")
        bits.append("s sort")
        bits.append("f/esc dismiss")
        hint.append(" • ".join(bits), style="dim")
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Toggling clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def _render_active_category(self, category) -> Text:
        if isinstance(category, MultiSelectFilterViewModel):
            return self._render_multiselect(category)
        # Defensive: unknown category type — paint a placeholder so we
        # don't blow up rendering. Concrete handling lands when a new
        # subclass is added.
        return Text(f"(no renderer for {type(category).__name__})", style="dim")

    def _render_multiselect(self, category: MultiSelectFilterViewModel) -> Text:
        # Cursor colour: bright gold on focus, dim otherwise. Same
        # convention as ``_SortBar``.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        for i, option in enumerate(category.options):
            is_cursor = i == category.cursor
            is_sel = category.is_selected(option)
            marker = "[x]" if is_sel else "[ ]"
            marker_style = "#5fd75f" if is_sel else "#787878"
            label_style = cursor_color if is_cursor else ""
            text.append(marker, style=marker_style)
            text.append(" ")
            text.append(option, style=label_style)
            if i < len(category.options) - 1:
                text.append("    ")
        return text

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cycle_category(self, direction: int) -> None:
        self._vm.filter_tab(direction)

    def action_cursor_left(self) -> None:
        self._vm.filter_move_cursor(-1)

    def action_cursor_right(self) -> None:
        self._vm.filter_move_cursor(1)

    def action_toggle(self) -> None:
        self._vm.filter_toggle_current()

    def action_reset(self) -> None:
        self._vm.filter_reset()

    def action_cancel(self) -> None:
        self._vm.cancel_filter()

    def action_request_sort(self) -> None:
        self._vm.request_sort()


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
    KnowledgeEntryBrowserPaneView #sort-bar {
        /* 2 lines of content (options + hint) plus the ``border-top``. */
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #sort-bar.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #sort-bar:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog {
        /* 3 lines of content (tabs + body + hint) plus the
           ``border-top``. */
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog:focus {
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
        # Same edge-detection pattern for the sort dialog. See the
        # ``_was_delete_pending`` note above for the rationale.
        self._was_sort_pending: bool = False
        # And the filter dialog.
        self._was_filter_pending: bool = False
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
        yield _SortBar(self._vm, id="sort-bar")
        yield _FilterDialog(self._vm, id="filter-dialog")
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
            # Topic column shows the topic name followed by " [{id}]"
            # in a fixed dim grey — matches the topic tree's hint style.
            # ``selectinload`` on ``list_entries_paginated`` ensures
            # ``entry.topic`` is populated before the session closes;
            # the defensive fallback to ``topic_id`` is here in case
            # something ever lands an entry whose topic FK isn't loaded.
            topic_name = entry.topic.name if entry.topic is not None else "?"
            topic_cell = Text.assemble(
                (topic_name, style),
                (f" [{entry.topic_id}]", "#787878"),
            )
            cells = (
                Text(marker, style=style),
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                topic_cell,
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

        # Three-dialog visibility + coordinated focus rescue. The
        # ``_was_*`` edge detectors mirror the ``_was_dirty`` pattern in
        # ``EntryDetailsView``; the open/close logic has to coordinate
        # across all three because a swap (e.g. ``s`` pressed while the
        # filter dialog is open) closes one and opens another in the
        # *same* refresh — running each dialog's focus rescue
        # independently lets the closing one's "restore focus to
        # table" overwrite the opening one's "focus dialog" grab.
        delete_dialog = self.query_one("#delete-confirm", _DeleteConfirm)
        sort_bar = self.query_one("#sort-bar", _SortBar)
        filter_dialog = self.query_one("#filter-dialog", _FilterDialog)

        delete_pending = self._vm.delete_pending
        sort_pending = self._vm.sort_pending
        filter_pending = self._vm.filter_pending

        delete_dialog.set_class(delete_pending, "-visible")
        sort_bar.set_class(sort_pending, "-visible")
        filter_dialog.set_class(filter_pending, "-visible")

        # Resolve focus once after all visibility flips are queued.
        # Opens beat closes — if any dialog just opened, grab focus to
        # it (priority order: filter, sort, delete, matching the VM's
        # mutual-exclusion preference). Otherwise, if at least one
        # dialog just closed *and* nothing is currently open, restore
        # focus to the table.
        just_opened_filter = filter_pending and not self._was_filter_pending
        just_opened_sort = sort_pending and not self._was_sort_pending
        just_opened_delete = delete_pending and not self._was_delete_pending
        just_closed_any = (
            (self._was_delete_pending and not delete_pending)
            or (self._was_sort_pending and not sort_pending)
            or (self._was_filter_pending and not filter_pending)
        )
        any_pending = delete_pending or sort_pending or filter_pending

        if just_opened_filter:
            filter_dialog.focus()
        elif just_opened_sort:
            sort_bar.focus()
        elif just_opened_delete:
            delete_dialog.focus()
        elif just_closed_any and not any_pending:
            try:
                self.query_one("#entries-table", DataTable).focus()
            except Exception:
                # Table may have been unmounted (e.g. pane swap
                # mid-close); let focus settle wherever Textual puts it.
                pass

        self._was_delete_pending = delete_pending
        self._was_sort_pending = sort_pending
        self._was_filter_pending = filter_pending

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
        table, but if a dialog is open we re-focus it instead so the
        user picks up where they left off after a tree side-trip
        (alt+left from a dialog hops back to the tree). The three
        dialogs are mutually exclusive at the VM level so this order
        of checks only documents priority."""
        if self._vm.filter_pending:
            try:
                self.query_one("#filter-dialog", _FilterDialog).focus()
                return
            except Exception:
                pass
        if self._vm.sort_pending:
            try:
                self.query_one("#sort-bar", _SortBar).focus()
                return
            except Exception:
                pass
        if self._vm.delete_pending:
            try:
                self.query_one("#delete-confirm", _DeleteConfirm).focus()
                return
            except Exception:
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
