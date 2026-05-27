"""KnowledgeEntryBrowserTabView — DataTable + details + status row + four pop-up dialogs (delete /
sort / filter / edit).

The tab view owns *interaction state*: which dialog is currently visible, where each dialog's
cursor lives, focus management on show/hide. The VM (see ``view_model.py``) owns *data facts* (the
loaded window, sort/search/filter values, selection) and the bulk-action API the dialogs eventually
invoke. The dialogs talk to the VM through that narrow surface (``set_sort``, ``set_type_filter``,
``delete_selected_entries``, ``change_topic_on_selected_entries``,
``change_type_on_selected_entries``) and Textual's focus mechanics carry keystrokes the rest of the
way.

Per-widget code lives in sibling modules — ``delete_dialog.py``, ``filter_dialog.py``,
``edit_dialog.py``, ``entry_content_preview.py``. The search bar is the shared generic
``SearchInput`` from ``rhizome.tui.widgets.search_input``, parameterised on
``KnowledgeEntryBrowserTabViewModel``; the sort dialog is the shared generic ``SortDialog``
from ``rhizome.tui.widgets.browser.sort_dialog``, specialised inline here as
``_EntriesSortDialog`` to surface the multi-select warning. This module keeps the tab
container itself plus the ``_EntriesTable`` subclass, which is tightly coupled to the tab's
dialog orchestration and focus walk.
"""

from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Input, Rule, Static, TextArea

from rhizome.db.models import EntryType

from ...search_input import SearchInput
from ..multi_selectable_table import MultiSelectableDataTable
from ..sort_dialog import SortDialog

from .delete_dialog import _DeleteConfirm
from .edit_dialog import _EditBar, _TypePickerScreen
from .entry_content_preview import _EntryContentPreview
from .entry_details import EntryDetailsView
from .filter_dialog import _FilterDialog
from .linked_flashcards import LinkedFlashcardsPanelView
from .view_model import KnowledgeEntryBrowserTabViewModel


class _EntriesSortDialog(SortDialog[KnowledgeEntryBrowserTabViewModel]):
    """Knowledge-entry-tab specialisation of ``SortDialog``. Surfaces a "Applying clears your
    selection." warning inline with the keybinding hint while multi-select is on — applying a
    sort clears the selection (rows reshuffle and selection-by-position loses meaning), so we
    give the user a heads-up before they commit.
    """

    def _extra_hint(self) -> Text | None:
        if self._vm.multi_select_active:
            return Text("Applying clears your selection.", style="#ff8787")
        return None

_DialogName = Literal["delete", "sort", "filter", "edit"]


class _EntriesTable(MultiSelectableDataTable[KnowledgeEntryBrowserTabViewModel]):
    """Entries-tab specialisation of ``MultiSelectableDataTable``. The base owns the
    ``space`` / ``shift+up`` / ``shift+down`` multi-select keybindings; this subclass adds
    auto-load-more pagination on cursor-down at the bottom edge. The global tab keys
    (``d`` / ``s`` / ``f`` / ``e`` / ``l`` / ``m``) live on
    ``KnowledgeEntryBrowserTabView`` so they fire from either table.
    """

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(view_model, **kwargs)
        self._tab = tab

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge: if the user is on the last loaded
        row and the VM still has more to fetch, await ``load_more`` first so the next
        ``super().action_cursor_down`` has somewhere to land. ``load_more`` is a no-op when
        nothing further is available or a fetch is already in flight, so this is safe to
        call without re-checking those conditions.

        The cursor advance happens *after* the await — by then the VM has appended rows +
        emitted dirty, ``_refresh`` ran in ``extend`` mode, and the table has the new rows
        mounted.
        """
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()


class KnowledgeEntryBrowserTabView(Vertical):
    """Tab view for ``KnowledgeEntryBrowserTabViewModel``: search bar + DataTable + status row,
    a details panel on the right (in ``ENTRIES``) or a linked-flashcards table (in
    ``LINKED_FLASHCARDS``), and four pop-up dialogs (delete / sort / filter / edit) along the
    bottom.

    Owns the dialog mutex via ``_active_dialog`` and the ``toggle_dialog`` / ``show_dialog`` /
    ``hide_dialog`` methods. The dialog widgets and the entries-table key bindings ask the tab to
    swap dialogs; the tab handles visibility (``-visible`` class) and focus rescue.
    """

    DEFAULT_CSS = """
    KnowledgeEntryBrowserTabView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    KnowledgeEntryBrowserTabView #tab-body {
        layout: horizontal;
        height: 1fr;
    }
    KnowledgeEntryBrowserTabView #tab-body-rule {
        margin: 0 0 0 0;
        color: #3a3a3a;
    }
    /* Left column of the tab body: search bar over the entries
       table. Width is set per-state below (60% in ENTRIES, 50% in
       LINKED_FLASHCARDS); the table fills its parent column. A 1-char
       right margin keeps the right-hand panel area (nav arrow + view)
       from sitting flush against the entries table. */
    KnowledgeEntryBrowserTabView #table-column {
        height: 1fr;
        layout: vertical;
        margin-right: 1;
    }
    KnowledgeEntryBrowserTabView #entries-table {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Entry-content preview sits below the entries table in the left column. Hidden by default
       (the ``ENTRIES`` state has the editable details panel on the right doing the same job); the
       ``-state-linked-flashcards`` rule below flips it on and rebalances the column to 2fr/1fr
       table/preview. */
    KnowledgeEntryBrowserTabView #entry-content-preview {
        display: none;
    }
    KnowledgeEntryBrowserTabView.-state-linked-flashcards #entries-table {
        height: 2fr;
    }
    KnowledgeEntryBrowserTabView.-state-linked-flashcards #entry-content-preview {
        display: block;
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Multi-select wash: keep the zebra alternation but shift both rows
       darker, so the table reads as muted-but-structured and the bright-
       green selected rows pop. */
    KnowledgeEntryBrowserTabView #entries-table.-multi-select {
        background: $surface-darken-2;
    }
    KnowledgeEntryBrowserTabView #entries-table.-multi-select > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    /* State-driven layout swap. Both right-hand views are mounted up front; the ``-state-*``
       class toggles which is visible and the corresponding widths. The visible right-hand view
       takes the remaining space (``1fr``). */
    KnowledgeEntryBrowserTabView.-state-entries #table-column {
        width: 60%;
    }
    KnowledgeEntryBrowserTabView.-state-entries EntryDetailsView {
        width: 1fr;
        height: 1fr;
        display: block;
    }
    KnowledgeEntryBrowserTabView.-state-entries LinkedFlashcardsPanelView {
        display: none;
    }
    KnowledgeEntryBrowserTabView.-state-linked-flashcards #table-column {
        width: 50%;
    }
    KnowledgeEntryBrowserTabView.-state-linked-flashcards LinkedFlashcardsPanelView {
        width: 1fr;
        height: 1fr;
        display: block;
    }
    KnowledgeEntryBrowserTabView.-state-linked-flashcards EntryDetailsView {
        display: none;
    }
    /* Status row at the bottom of ``#table-column`` so it aligns with the linked-flashcards docked
       status row inside its own column. */
    KnowledgeEntryBrowserTabView #tab-status {
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    KnowledgeEntryBrowserTabView #delete-confirm {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserTabView #delete-confirm.-visible {
        display: block;
    }
    KnowledgeEntryBrowserTabView #delete-confirm:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserTabView #sort-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserTabView #sort-bar.-visible {
        display: block;
    }
    KnowledgeEntryBrowserTabView #sort-bar:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserTabView #filter-dialog {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserTabView #filter-dialog.-visible {
        display: block;
    }
    KnowledgeEntryBrowserTabView #filter-dialog:focus {
        border-top: solid $accent;
    }
    /* The filter dialog is a Vertical with three rows: type row, flashcard row (a Horizontal
       containing the radios Static + the compact One-of Input), and hint row. Each Static row is
       1 line; with the dialog's own top border this lands at the 4-line total declared above. */
    KnowledgeEntryBrowserTabView #filter-dialog #type-row,
    KnowledgeEntryBrowserTabView #filter-dialog #hint-row {
        height: 1;
        width: 1fr;
    }
    KnowledgeEntryBrowserTabView #filter-dialog #flashcard-row {
        height: 1;
        width: 1fr;
    }
    KnowledgeEntryBrowserTabView #filter-dialog #flashcard-radios {
        height: 1;
        width: auto;
    }
    KnowledgeEntryBrowserTabView #filter-dialog #one-of-input {
        height: 1;
        width: 25;
        padding: 0;
        margin: 0;
        border: none;
        /* Slightly-lightened background so the input area reads as a distinct field even when
           unfocused. ``background-tint`` overlays the parent surface rather than replacing it, so
           the dialog's own colour bleeds through correctly. */
        background: transparent;
        background-tint: $foreground 8%;
    }
    KnowledgeEntryBrowserTabView #filter-dialog #one-of-input:focus {
        background-tint: $foreground 15%;
    }
    KnowledgeEntryBrowserTabView #filter-dialog #one-of-close-bracket {
        height: 1;
        width: 1;
    }
    KnowledgeEntryBrowserTabView #edit-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserTabView #edit-bar.-visible {
        display: block;
    }
    KnowledgeEntryBrowserTabView #edit-bar:focus {
        border-top: solid $accent;
    }
    /* Permanent keybindings line at the very bottom of the tab. Left-aligned, single line, lists
       the global mode/navigation keys so they're discoverable without an explicit help toggle.
       A thin Rule sits above it as a visual separator from whichever dialog (or the status row)
       sits directly above. */
    KnowledgeEntryBrowserTabView #tab-keybindings-rule {
        margin: 1 0 0 0;
        color: #3a3a3a;
    }
    KnowledgeEntryBrowserTabView #tab-keybindings {
        height: auto;
        text-align: left;
        padding: 0 1;
    }
    """

    # Max display width for the title column. Anything longer is truncated by ``DataTable`` (with an
    # ellipsis).
    _TITLE_COLUMN_WIDTH = 50

    # Maps dialog name → (widget id, widget class). Used by ``toggle_dialog`` /
    # ``_refresh_dialog_visibility`` to dispatch generically.
    _DIALOG_WIDGETS: tuple[tuple[_DialogName, str], ...] = (
        ("delete", "#delete-confirm"),
        ("sort", "#sort-bar"),
        ("filter", "#filter-dialog"),
        ("edit", "#edit-bar"),
    )

    # Global tab keys — fire from either table (and from anywhere else in the tab that isn't an
    # ``Input`` / ``TextArea``). Bound here rather than on ``_EntriesTable`` so the linked-
    # flashcards table picks them up for free. Each open dialog has its own ``d`` / ``s`` / ``f``
    # / ``e`` bindings for sibling swap; the focused widget's bindings take precedence over an
    # ancestor's, so the dialog rules still win while a dialog is focused.
    BINDINGS = [
        Binding("d", "tab_toggle_dialog('delete')", show=False),
        Binding("s", "tab_toggle_dialog('sort')", show=False),
        Binding("f", "tab_toggle_dialog('filter')", show=False),
        Binding("e", "tab_toggle_dialog('edit')", show=False),
        Binding("l", "tab_toggle_relink", show=False),
        Binding("m", "tab_toggle_multi_select", show=False),
        Binding("tab", "tab_cycle_mode", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Which dialog (if any) is currently visible. The mutex: at most one is shown at a time.
        # Mutators run through ``toggle_dialog`` / ``hide_dialog`` so visibility, focus, and
        # ``prepare_for_show`` stay coordinated.
        self._active_dialog: _DialogName | None = None
        # Tracks the VM's state at the last refresh so a state transition can auto-dismiss any open
        # dialog (dialogs that depend on the current state — selection actions, sort axes — aren't
        # generally meaningful across a layout switch).
        self._last_state: KnowledgeEntryBrowserTabViewModel.State | None = None
        # Signature of the entries list at the last refresh — a tuple of entry ids in display order.
        # Used by ``_refresh`` to decide between a full ``clear()`` + rebuild (when row identity has
        # actually changed: refetch, delete, load_more) and a cheap in-place ``update_cell_at`` pass
        # (when only styles or markers changed: mode toggle, selection toggle, post-edit content
        # mutation). The in-place path preserves ``DataTable``'s scroll position and cursor.
        self._last_row_signature: tuple[int, ...] | None = None

    def compose(self):
        table = _EntriesTable(
            self._vm, self,
            id="entries-table", cursor_type="row", zebra_stripes=True,
        )
        # The leading "sel" column is always present (we can't add or drop columns cleanly after
        # construction). When multi-select is off the column renders empty; when on, each row shows
        # ``[ ]`` or ``[x]``.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        table.add_column("flashcards")
        with Horizontal(id="tab-body"):
            with Vertical(id="table-column"):
                yield SearchInput[KnowledgeEntryBrowserTabViewModel](
                    self._vm, id="search-input",
                )
                yield table
                # Preview only renders in ``LINKED_FLASHCARDS`` — CSS toggles ``display`` based on
                # the parent's ``-state-*`` class.
                yield _EntryContentPreview(self._vm, id="entry-content-preview")
                yield Static("", id="tab-status")

            yield Rule(orientation="vertical", line_style="solid", id="tab-body-rule")
            # Both right-hand views are mounted up front and shown / hidden via the ``-state-*``
            # class on the parent. Mounting them once avoids the cost of re-subscribing each
            # child view's ``vm.dirty`` callback on every state flip.
            yield EntryDetailsView(self._vm.details)
            yield LinkedFlashcardsPanelView(self._vm.linked_flashcards)
        yield _DeleteConfirm(self._vm, self, id="delete-confirm")
        yield _EntriesSortDialog(self._vm, on_close=self.hide_dialog, id="sort-bar")
        yield _FilterDialog(self._vm, self, id="filter-dialog")
        yield _EditBar(self._vm, self, id="edit-bar")
        # Permanent keybindings line at the very bottom — surfaces the global mode/navigation
        # keys so they're discoverable without an explicit help toggle. The Rule above acts as a
        # visual separator from whichever dialog (or status row) sits directly above.
        yield Rule(line_style="solid", id="tab-keybindings-rule")
        yield Static(self._keybindings_text(), id="tab-keybindings")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # If the VM already has data (it was bootstrapped before the view mounted), paint it on
        # first frame instead of waiting for the next dirty.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Bottom keybindings line
    # ------------------------------------------------------------------

    def _keybindings_text(self) -> str:
        """Permanent left-aligned listing of the tab's global mode/navigation keys. The keybinding
        glyph renders a touch brighter than the action label so it pops out of the row; per-dialog
        keys (← / → / enter / esc inside an open dialog) are documented by the dialog widgets
        themselves."""
        rows = [
            ("e", "edit"),
            ("f", "filter"),
            ("s", "sort"),
            ("d", "delete"),
            ("l", "link flashcards"),
            ("m", "multi-select"),
            ("tab", "cycle mode"),
            ("alt+←↑→↓", "navigate"),
        ]
        return "   ".join(
            f"[#a0a0a0]{key}[/] [#707070]{label}[/]" for key, label in rows
        )

    # ------------------------------------------------------------------
    # Global tab actions (d / s / f / e / l / m) — see ``BINDINGS`` above
    # ------------------------------------------------------------------
    #
    # Each defers to the existing dialog / relink / multi-select methods. The gate skips the action
    # when an ``Input`` / ``TextArea`` is focused so typing doesn't trip the keybinding (defensive —
    # Input/TextArea already consume printable keys via their own ``_on_key`` handlers, but the
    # check makes the intent explicit and survives any future refactor that changes their key
    # plumbing).

    def _typing_active(self) -> bool:
        """Whether the currently-focused widget is an editable text field. Used by the global tab
        actions to bail rather than swallow a keystroke meant for the editor."""
        focused = self.screen.focused if self.screen else None
        return isinstance(focused, (Input, TextArea))

    def action_tab_toggle_dialog(self, name: str) -> None:
        if self._typing_active():
            return
        self.toggle_dialog(name)  # type: ignore[arg-type]

    def action_tab_toggle_relink(self) -> None:
        if self._typing_active():
            return
        self.toggle_relink_mode()

    def action_tab_toggle_multi_select(self) -> None:
        if self._typing_active():
            return
        self._vm.toggle_multi_select()

    def action_tab_cycle_mode(self) -> None:
        """Flip between the entry-details and linked-flashcards right-panel modes. Two-state
        toggle for now; if a third state lands, replace with an explicit picker."""
        if self._typing_active():
            return
        current = self._vm.state
        target = (
            self._vm.State.LINKED_FLASHCARDS
            if current is self._vm.State.ENTRIES
            else self._vm.State.ENTRIES
        )
        self._vm.transition_to(target)

    # ------------------------------------------------------------------
    # Dialog orchestration
    # ------------------------------------------------------------------

    def toggle_dialog(self, name: _DialogName) -> None:
        """Toggle the named dialog. If it's already shown, hide it; otherwise show it (hiding any
        other). Called from the entries table's key bindings (d/e/f/s) and from sibling-dialog swap
        actions."""
        if self._active_dialog == name:
            self.hide_dialog()
        else:
            self.show_dialog(name)

    def show_dialog(self, name: _DialogName) -> None:
        """Show the named dialog and hide any other. No-op if it's already showing."""
        if self._active_dialog == name:
            return
        self._active_dialog = name
        self._refresh_dialog_visibility()

    def hide_dialog(self) -> None:
        """Hide whichever dialog is currently shown (if any). Refocuses the entries table."""
        if self._active_dialog is None:
            return
        self._active_dialog = None
        self._refresh_dialog_visibility()

    def _refresh_dialog_visibility(self) -> None:
        """Apply the ``-visible`` class to the active dialog (and remove it from the others), then
        run focus rescue: focus the newly-active dialog, or fall back to the entries table when
        nothing's active. Called whenever ``_active_dialog`` flips."""
        for name, widget_id in self._DIALOG_WIDGETS:
            try:
                # ``Widget`` (rather than ``Static``) because the filter dialog is a ``Vertical``
                # container — the other three dialogs are still Statics, but the loop is generic.
                widget = self.query_one(widget_id, Widget)
            except Exception:
                # Compose hasn't finished yet — skip; mount-time _refresh will catch up.
                continue
            is_visible = name == self._active_dialog
            widget.set_class(is_visible, "-visible")
            if is_visible:
                prepare = getattr(widget, "prepare_for_show", None)
                if prepare is not None:
                    prepare()
                widget.focus()
        if self._active_dialog is None:
            try:
                self.query_one("#entries-table", DataTable).focus()
            except Exception:
                # Table may have been unmounted (e.g. tab swap mid-close); let focus settle wherever
                # Textual puts it.
                pass

    # ------------------------------------------------------------------
    # Relink mode entry point
    # ------------------------------------------------------------------

    def toggle_relink_mode(self) -> None:
        """View-layer wrapper around the VM's relink mutators. Closes any open dialog (mutex lives
        here, not the VM) and then routes to ``enter_relink_mode`` or ``exit_relink_mode`` on the
        VM based on the panel's current state. Called from the entries-table ``l`` binding."""
        self.hide_dialog()
        if self._vm.linked_flashcards.relink_mode:
            self._vm.exit_relink_mode()
        else:
            self._vm.enter_relink_mode()

    # ------------------------------------------------------------------
    # Selection-target helper (used by dialog rendering and dispatchers)
    # ------------------------------------------------------------------

    def selection_target_count(self) -> int:
        """Count of entries the current "selected" action would act on. In multi-select mode that's
        ``selected_ids``; in single-select mode it's the cursor entry (or zero if the window is
        empty)."""
        if self._vm.multi_select_active:
            return len(self._vm.selected_ids)
        return 1 if self._vm.entries else 0

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        mode = self._vm.multi_select_active
        # ``-multi-select`` triggers the CSS that darkens the zebra-row palette while the user is
        # picking.
        table.set_class(mode, "-multi-select")

        # Apply the state class. The CSS rules keyed off ``.-state-entries`` /
        # ``.-state-linked-flashcards`` swap which right-hand view is visible and the corresponding
        # left-column width (60/40 vs 50/50).
        state = self._vm.state
        self.set_class(state is self._vm.State.ENTRIES, "-state-entries")
        self.set_class(state is self._vm.State.LINKED_FLASHCARDS, "-state-linked-flashcards")

        # State transition closes whichever dialog is open — its targets / sort axes / filter
        # options aren't generally meaningful across a layout switch, and the user is doing a bigger
        # navigation gesture than a dialog dismissal.
        if self._last_state is not None and state != self._last_state:
            if self._active_dialog is not None:
                self.hide_dialog()
        self._last_state = state

        # Three refresh paths, picked by comparing the new id-tuple to the previously-rendered one:
        #
        #   * ``extend`` — old tuple is a prefix of the new one, length grew. ``load_more`` appends
        #     rows; we ``add_row`` only the new tail. No ``clear``, no cursor restore, scroll stays
        #     where the user left it.
        #   * ``inplace`` — same tuple. Pure style/marker churn (multi-select toggle, selection
        #     toggle, post-edit content mutation). ``update_cell_at`` per cell preserves scroll +
        #     cursor.
        #   * ``rebuild`` — anything else (refetch, delete, reorder). ``clear`` + ``add_row``,
        #     restore cursor.
        new_signature = tuple(e.id for e in self._vm.entries)
        old_signature = self._last_row_signature
        if new_signature == old_signature:
            path = "inplace"
            start = 0
        elif (
            old_signature is not None
            and len(new_signature) > len(old_signature)
            and new_signature[: len(old_signature)] == old_signature
        ):
            path = "extend"
            start = len(old_signature)
        else:
            path = "rebuild"
            start = 0
            table.clear()

        for i in range(start, len(self._vm.entries)):
            entry = self._vm.entries[i]
            type_str = entry.entry_type.value if entry.entry_type is not None else "—"
            # Three colouring regimes:
            #   * not multi-select: zebra-pair text (odd rows dim) so the stripe background shows
            #     through evenly.
            #   * multi-select, not selected: same zebra-pair pattern but with both colours shifted
            #     darker — so the whole table reads as muted-but-structured.
            #   * multi-select, selected: bright green + bold to pop against the dimmed sea around
            #     them.
            selected = mode and entry.id in self._vm.selected_ids
            if selected:
                style = "bold #5fd75f"
            elif mode:
                style = "#787878" if i % 2 else "#a0a0a0"
            else:
                style = "#a0a0a0" if i % 2 else ""
            marker = ("[x]" if selected else "[ ]") if mode else ""
            # Topic column shows the topic name followed by " [{id}]" in a fixed dim grey — matches
            # the topic tree's hint style. The defensive fallback to ``topic_id`` is here in case
            # something ever lands an entry whose topic FK isn't loaded.
            topic_name = entry.topic.name if entry.topic is not None else "?"
            topic_cell = Text.assemble(
                (topic_name, style),
                (f" [{entry.topic_id}]", "#787878"),
            )
            # Flashcard ids are sorted for stable display order — the ``flashcard_entries``
            # collection is loaded via ``selectinload``, which doesn't promise any particular order.
            fc_ids = sorted(fe.flashcard_id for fe in entry.flashcard_entries)
            fc_str = ", ".join(str(i) for i in fc_ids) if fc_ids else "—"
            cells = (
                Text(marker, style=style),
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                topic_cell,
                Text(fc_str, style=style),
            )
            if path == "inplace":
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
            else:
                table.add_row(*cells, key=str(entry.id))
        self._last_row_signature = new_signature

        # After a rebuild, ``table.clear()`` reset the table cursor to row 0. Push the VM's cursor
        # back into the table so the highlight lands on the row the VM expects. ``move_cursor``
        # fires ``RowHighlighted``, which round-trips into ``vm.set_cursor`` — the
        # early-return-on-equality there keeps this from looping. On ``extend`` / ``inplace`` the
        # cursor was never disturbed, so we skip.
        if (
            path == "rebuild"
            and self._vm.entries
            and 0 <= self._vm.cursor < len(self._vm.entries)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#tab-status", Static)
        status.update(self._format_status())

    # ------------------------------------------------------------------
    # Cross-region focus (driven by ``BrowserView``'s alt+arrow bindings)
    # ------------------------------------------------------------------
    #
    # The tab participates in a directional focus graph spanning the topic tree, the entries-side
    # widgets (search, table, title, content, modification-accept), the dialogs, and the linked-
    # flashcards-side widgets (search, table). ``nav_<dir>`` resolves a single step in the named
    # direction by:
    #   1. Naming the currently-focused node via ``_focused_node``.
    #   2. Looking up its outgoing edge for the direction in the explicit if-chain below.
    #   3. Focusing the target via ``_focus_node`` (which gates on present-ness).
    #
    # ``nav_left`` returns the sentinel ``"topic_tree"`` for transitions that escape the tab —
    # ``BrowserView`` catches that and focuses the tree. Every other case returns a bool.

    # Node-name → ``query_one`` selector for the focusable widget that represents that node. The
    # dialog node is handled separately because its target widget depends on ``_active_dialog``.
    _NODE_TO_WIDGET_ID: dict[str, str] = {
        "entry_table": "entries-table",
        "entry_search": "search-input",
        "entry_title": "details-title",
        "entry_content": "details-content",
        "entry_modification_accept": "details-choices",
        "flashcard_table": "linked-flashcards-table",
        "flashcard_search": "linked-flashcards-search-input",
        "relink_choices": "linked-flashcards-relink-choices",
    }

    def focus_first(self) -> None:
        """Entry point when ``BrowserView`` enters the tab from the tree. Always lands on the
        entries table — the leftmost region in the focus graph — regardless of whether a dialog
        happens to be open. The user can step into the dialog from there with alt+down."""
        self.query_one("#entries-table", DataTable).focus()

    def _focused_node(self) -> str | None:
        """Name the currently-focused widget within the tab, or ``None`` if focus is outside the
        tab / on something we don't route. Dialogs collapse to a single ``"dialog"`` name (the
        outgoing edges are the same regardless of which of the four is shown)."""
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return None
        if self._active_dialog is not None:
            widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
            try:
                if focused is self.query_one(widget_id, Widget):
                    return "dialog"
            except Exception:
                pass
        fid = focused.id
        if fid == "entries-table":
            return "entry_table"
        if fid == "search-input":
            return "entry_search"
        if fid == "details-title":
            return "entry_title"
        if fid == "details-content":
            return "entry_content"
        if fid == "details-choices":
            return "entry_modification_accept"
        if fid == "linked-flashcards-table":
            return "flashcard_table"
        if fid == "linked-flashcards-search-input":
            return "flashcard_search"
        if fid == "linked-flashcards-relink-choices":
            return "relink_choices"
        return None

    def _node_present(self, node: str) -> bool:
        """Whether ``node`` is currently in the focus graph. Transitions to absent nodes silently
        no-op. The frozen details panel (multi-select on the entries side) is treated as absent —
        the TextAreas are read-only and the choices widget is hidden, so there's nothing useful
        to land on."""
        if node == "dialog":
            return self._active_dialog is not None
        state = self._vm.state
        frozen = self._vm.multi_select_active
        if node in ("entry_title", "entry_content"):
            return state is self._vm.State.ENTRIES and not frozen
        if node == "entry_modification_accept":
            return (
                state is self._vm.State.ENTRIES
                and not frozen
                and self._vm.details.is_dirty
            )
        if node in ("flashcard_table", "flashcard_search"):
            return state is self._vm.State.LINKED_FLASHCARDS
        if node == "relink_choices":
            return (
                state is self._vm.State.LINKED_FLASHCARDS
                and self._vm.linked_flashcards.is_relink_dirty
            )
        # entry_table / entry_search are always present.
        return node in ("entry_table", "entry_search")

    def _focus_node(self, node: str) -> bool:
        """Focus the widget corresponding to ``node``. Returns ``False`` and skips the focus call
        if the node isn't currently in the graph (i.e. its widget is hidden by the state class or
        the multi-select freeze)."""
        if not self._node_present(node):
            return False
        if node == "dialog":
            assert self._active_dialog is not None  # implied by _node_present
            widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
            try:
                self.query_one(widget_id, Widget).focus()
                return True
            except Exception:
                return False
        target_id = self._NODE_TO_WIDGET_ID[node]
        try:
            self.query_one(f"#{target_id}").focus()
            return True
        except Exception:
            return False

    def nav_up(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node == "dialog":
            return self._focus_node("entry_table")
        if node == "entry_table":
            return self._focus_node("entry_search")
        if node == "flashcard_table":
            return self._focus_node("flashcard_search")
        if node == "relink_choices":
            return self._focus_node("flashcard_table")
        if node == "entry_modification_accept":
            return self._focus_node("entry_content")
        if node == "entry_content":
            return self._focus_node("entry_title")
        if node == "entry_title":
            return self._focus_node("entry_search")
        return False

    def nav_down(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node == "entry_search":
            return self._focus_node("entry_table")
        if node == "flashcard_search":
            return self._focus_node("flashcard_table")
        if node == "entry_table":
            return self._focus_node("dialog")
        if node == "flashcard_table":
            # Mirrors entry_content's fall-through: relink Accept/Cancel takes priority while
            # dirty, otherwise drop to the dialog if one is open.
            return self._focus_node("relink_choices") or self._focus_node("dialog")
        if node == "relink_choices":
            return self._focus_node("dialog")
        if node == "entry_title":
            return self._focus_node("entry_content")
        if node == "entry_content":
            # Fall through: accept/cancel takes priority while the panel is dirty; otherwise drop
            # to the dialog if one is open. Both targets are gated by ``_node_present``, so this
            # is just two short-circuited focus attempts.
            return self._focus_node("entry_modification_accept") or self._focus_node("dialog")
        if node == "entry_modification_accept":
            return self._focus_node("dialog")
        return False

    def nav_left(self) -> bool | str:
        """Returns ``True`` if focus moved inside the tab, the sentinel ``"topic_tree"`` to ask
        ``BrowserView`` to hand focus to the tree, or ``False`` if there's no outgoing edge."""
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node in ("entry_search", "entry_table", "dialog"):
            return "topic_tree"
        if node == "entry_title":
            return self._focus_node("entry_search")
        if node in ("entry_content", "entry_modification_accept"):
            return self._focus_node("entry_table")
        if node == "flashcard_search":
            return self._focus_node("entry_search")
        if node == "flashcard_table":
            return self._focus_node("entry_table")
        if node == "relink_choices":
            return self._focus_node("entry_table")
        return False

    def nav_right(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        state = self._vm.state
        if node == "entry_search":
            if state is self._vm.State.ENTRIES:
                return self._focus_node("entry_title")
            if state is self._vm.State.LINKED_FLASHCARDS:
                return self._focus_node("flashcard_search")
        if node == "entry_table":
            if state is self._vm.State.ENTRIES:
                return self._focus_node("entry_content")
            if state is self._vm.State.LINKED_FLASHCARDS:
                return self._focus_node("flashcard_table")
        return False

    # ------------------------------------------------------------------
    # Edit-dialog choice dispatch
    # ------------------------------------------------------------------
    #
    # Called from ``_EditBar.action_select`` with the chosen option string. Two of the choices
    # (``change topic`` / ``change type``) open modal screens; ``edit title`` / ``edit content``
    # focus a TextArea in the details panel; ``delete`` swaps to the delete confirm dialog.

    async def handle_edit_choice(self, choice: str) -> None:
        if choice == "change topic":
            await self._dispatch_change_topic()
        elif choice == "change type":
            await self._dispatch_change_type()
        elif choice == "edit title":
            self._dispatch_focus_details_field("details-title")
        elif choice == "edit content":
            self._dispatch_focus_details_field("details-content")
        elif choice == "delete":
            self.show_dialog("delete")

    async def _dispatch_change_topic(self) -> None:
        """Open ``TopicSelectorScreen``; on dismiss apply the choice via the VM. No-op if there's
        nothing to act on (empty selection or empty window)."""
        # Local import to avoid circulating tui.screens through the widget module at import time —
        # matches the pattern already used in commit_proposal/view.py.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        if self.selection_target_count() == 0:
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                # User cancelled — keep the edit bar open so they can pick a different action.
                try:
                    self.query_one("#edit-bar", _EditBar).focus()
                except Exception:
                    pass
                return
            topic_id, _ = result
            self.run_worker(
                self._vm.change_topic_on_selected_entries(topic_id), exclusive=False,
            )

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._vm.session_factory),
            callback=on_dismiss,
        )

    async def _dispatch_change_type(self) -> None:
        """Open the inline ``_TypePickerScreen``; on dismiss apply via the VM. Lands the modal's
        cursor on the cursor entry's current type (single-select only — multi-select has no single
        "current" to land on)."""
        if self.selection_target_count() == 0:
            return

        current: EntryType | None = None
        if not self._vm.multi_select_active and self._vm.entries:
            current = self._vm.entries[self._vm.cursor].entry_type

        def on_dismiss(result: EntryType | None) -> None:
            if result is None:
                try:
                    self.query_one("#edit-bar", _EditBar).focus()
                except Exception:
                    pass
                return
            self.run_worker(
                self._vm.change_type_on_selected_entries(result), exclusive=False,
            )

        self.app.push_screen(_TypePickerScreen(current=current), callback=on_dismiss)

    def _dispatch_focus_details_field(self, widget_id: str) -> None:
        """Edit title / edit content: dismiss the edit bar and focus the target TextArea in the
        details panel. Single-select only — the option list excludes these in multi-select mode."""
        self.hide_dialog()
        try:
            target = self.query_one(f"#{widget_id}", TextArea)
        except Exception:
            return
        target.focus()

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Table cursor moved — push the row index into the VM.

        The VM's ``set_cursor`` no-ops if the index is unchanged, so this is safe to fire from
        programmatic ``move_cursor`` calls during ``_refresh`` (and from the initial mount, where
        the table seeds its cursor to row 0).
        """
        if event.data_table.id != "entries-table":
            return
        self._vm.set_cursor(event.cursor_row)

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        if self._vm.multi_select_active:
            # Multi-select takes over the status line — the "N of M" hint is still useful but
            # secondary, so we lead with the selection count.
            count = len(self._vm.selected_ids)
            noun = "entry" if count == 1 else "entries"
            return f"multi-select: {count} {noun} selected (m to exit, space to toggle)"
        total = self._vm.total
        loaded = len(self._vm.entries)
        if total is None:
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
