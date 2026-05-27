"""KnowledgeEntryBrowserTabView — DataTable + details + status row + four pop-up dialogs (delete /
sort / filter / edit).

The tab view owns *interaction state*: which dialog is currently visible, where each dialog's
cursor lives, focus management on show/hide. The VM (see ``view_model.py``) owns *data facts* (the
loaded window, sort/search/filter values, selection) and the bulk-action API the dialogs eventually
invoke. The dialogs talk to the VM through that narrow surface (``set_sort``, ``set_type_filter``,
``delete_selected_entries``, ``change_topic_on_selected_entries``,
``change_type_on_selected_entries``) and Textual's focus mechanics carry keystrokes the rest of the
way.

Per-widget code lives in sibling modules — ``search_input.py``, ``delete_dialog.py``,
``sort_dialog.py``, ``filter_dialog.py``, ``edit_dialog.py``, ``entry_content_preview.py``. This
module keeps the tab container itself plus the ``_EntriesTable`` subclass, which is tightly
coupled to the tab's dialog orchestration and focus walk.
"""

from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Static, TextArea

from rhizome.db.models import EntryType

from .delete_dialog import _DeleteConfirm
from .edit_dialog import _EditBar, _TypePickerScreen
from .entry_content_preview import _EntryContentPreview
from .entry_details import EntryDetailsView
from .filter_dialog import _FilterDialog
from .linked_flashcards import LinkedFlashcardsPanelView
from .search_input import _SearchInput
from .sort_dialog import _SortBar
from .view_model import KnowledgeEntryBrowserTabViewModel

_DialogName = Literal["delete", "sort", "filter", "edit"]


class _EntriesTable(DataTable):
    """``DataTable`` subclass that owns the multi-select keybindings and the dialog-toggle
    keybindings.

    Lives here rather than as standalone bindings on the parent view so the keys only fire when the
    table is focused — ``m`` and ``space`` on the details panel's ``TextArea``s would otherwise have
    to be suppressed. Selection actions delegate straight to the tab VM; dialog-toggle actions ask
    the parent tab to show/hide the named dialog.
    """

    BINDINGS = [
        Binding("m", "toggle_multi_select", show=False),
        Binding("space", "toggle_selection", show=False),
        # ``shift+up`` / ``shift+down`` are range-select sugar: add the cursor row to the selection
        # (idempotent) and step the cursor in one keystroke. Held-key terminal repeat makes "hold
        # shift, hold down" sweep a contiguous block. No-op outside multi-select (the VM guards).
        # Bound here rather than as ``"shift+up,shift+down"`` action pairs because each direction
        # needs its own cursor step.
        Binding("shift+down", "select_down", show=False),
        Binding("shift+up", "select_up", show=False),
        # ``d`` / ``s`` / ``f`` / ``e`` toggle the four dialogs. The tab owns which is currently
        # shown and runs the mutex (showing one hides the others).
        Binding("d", "toggle_dialog('delete')", show=False),
        Binding("s", "toggle_dialog('sort')", show=False),
        Binding("f", "toggle_dialog('filter')", show=False),
        Binding("e", "toggle_dialog('edit')", show=False),
        # ``ctrl+f`` flips the tab between ``ENTRIES`` (default) and ``LINKED_FLASHCARDS`` views.
        # First-pass binding — the user can refine the keystroke later. Lives on the table (not
        # the parent tab with priority) so it doesn't compete with ``ctrl+f`` in editing surfaces.
        Binding("ctrl+f", "toggle_state", show=False),
        # ``l`` enters / toggles relink mode. One motion from anywhere in the entries tab: drops
        # multi-select if on, flips into ``LINKED_FLASHCARDS`` if not already, and turns on the
        # panel's relink selection. Pressing again exits relink (stays in LINKED_FLASHCARDS).
        Binding("l", "toggle_relink", show=False),
        # ``h`` toggles the help section at the bottom of the tab. View-side concern — no VM
        # plumbing — see ``KnowledgeEntryBrowserTabView.toggle_help``.
        Binding("h", "toggle_help", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._tab = tab

    def action_toggle_multi_select(self) -> None:
        self._vm.toggle_multi_select()

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    async def action_select_down(self) -> None:
        """Add the current row to the selection, then step the cursor down. Cursor step uses
        ``action_cursor_down`` (our overridden async version, which also handles the load-more-at-
        bottom case) so the usual ``RowHighlighted`` event fires and the VM cursor stays in sync."""
        self._vm.add_current_to_selection()
        await self.action_cursor_down()

    def action_select_up(self) -> None:
        self._vm.add_current_to_selection()
        self.action_cursor_up()

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge: if the user is on the last loaded row and
        the VM still has more to fetch, await ``load_more`` first so the next
        ``super().action_cursor_down`` has somewhere to land. ``load_more`` is a no-op when nothing
        further is available or a fetch is already in flight, so this is safe to call without
        re-checking those conditions.

        The cursor advance happens *after* the await — by then the VM has appended rows + emitted
        dirty, ``_refresh`` ran in ``extend`` mode, and the table has the new rows mounted.
        """
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_toggle_dialog(self, name: str) -> None:
        self._tab.toggle_dialog(name)  # type: ignore[arg-type]

    def action_toggle_state(self) -> None:
        # Two-state toggle for now; if a third state lands, replace this with an explicit picker.
        current = self._vm.state
        target = (
            self._vm.State.LINKED_FLASHCARDS
            if current is self._vm.State.ENTRIES
            else self._vm.State.ENTRIES
        )
        self._vm.transition_to(target)

    def action_toggle_relink(self) -> None:
        self._tab.toggle_relink_mode()

    def action_toggle_help(self) -> None:
        self._tab.toggle_help()


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
    /* Left column of the tab body: search bar over the entries
       table. Width is set per-state below (60% in ENTRIES, 50% in
       LINKED_FLASHCARDS); the table fills its parent column. */
    KnowledgeEntryBrowserTabView #table-column {
        height: 1fr;
        layout: vertical;
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
    /* State-driven layout swap. Both right-hand views are mounted up
       front; the ``-state-*`` class toggles which is visible and the
       corresponding widths. */
    KnowledgeEntryBrowserTabView.-state-entries #table-column {
        width: 60%;
    }
    KnowledgeEntryBrowserTabView.-state-entries EntryDetailsView {
        width: 40%;
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
        width: 50%;
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
    /* Help section at the very bottom of the tab. The hint is always present (1 line); the
       full help expands above it when ``_help_visible`` flips on. View-side state, mirrors
       the FlashcardReview pattern visually. */
    KnowledgeEntryBrowserTabView #tab-help {
        height: auto;
        text-align: center;
        margin: 1 0 0 0;
        color: rgb(80,80,80);
        padding: 0 1;
        display: none;
    }
    KnowledgeEntryBrowserTabView #tab-help.-visible {
        display: block;
    }
    KnowledgeEntryBrowserTabView #tab-help-hint {
        height: 1;
        text-align: right;
        color: rgb(80,80,80);
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
        # Help section toggled by ``h`` on the entries table. View-side state — pure UI, no
        # data-model meaning, so it lives on the view rather than the VM. Mirrors the
        # FlashcardReview help pattern visually (a permanent hint at the very bottom plus an
        # expandable full-bindings list above it).
        self._help_visible: bool = False

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
                yield _SearchInput(self._vm, id="search-input")
                yield table
                # Preview only renders in ``LINKED_FLASHCARDS`` — CSS toggles ``display`` based on
                # the parent's ``-state-*`` class.
                yield _EntryContentPreview(self._vm, id="entry-content-preview")
                yield Static("", id="tab-status")

            # Both right-hand views are mounted up front and shown / hidden via the ``-state-*``
            # class on the parent. Mounting them once avoids the cost of re-subscribing each child
            # view's ``vm.dirty`` callback on every state flip.
            yield EntryDetailsView(self._vm.details)
            yield LinkedFlashcardsPanelView(self._vm.linked_flashcards)
        yield _DeleteConfirm(self._vm, self, id="delete-confirm")
        yield _SortBar(self._vm, self, id="sort-bar")
        yield _FilterDialog(self._vm, self, id="filter-dialog")
        yield _EditBar(self._vm, self, id="edit-bar")
        # Help section at the very bottom. Order matters: the full help sits above the hint
        # so when it expands it pushes the hint down rather than displacing it. Hint is
        # always 1 line; full help is display: none until ``_help_visible`` flips on.
        yield Static("", id="tab-help")
        yield Static("", id="tab-help-hint")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # If the VM already has data (it was bootstrapped before the view mounted), paint it on
        # first frame instead of waiting for the next dirty.
        self._refresh()
        # Initial help hint paint — independent of VM state, so a dedicated call instead of
        # piggybacking on ``_refresh`` (which fires only on ``vm.dirty``).
        self._refresh_help()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Help section (view-side)
    # ------------------------------------------------------------------

    def toggle_help(self) -> None:
        """Flip the help section. View-side state, so this skips ``vm.dirty`` — it just updates
        the two help widgets directly."""
        self._help_visible = not self._help_visible
        self._refresh_help()

    def _refresh_help(self) -> None:
        """Paint the help hint (always 1 line) and the full help (expanded only when
        ``_help_visible``). Called from ``on_mount`` for initial paint and from
        ``toggle_help`` when ``h`` is pressed. Mirrors the FlashcardReview pattern: the hint
        blanks while the full is expanded, and the full reveals via the ``.-visible`` class."""
        try:
            hint = self.query_one("#tab-help-hint", Static)
            full = self.query_one("#tab-help", Static)
        except Exception:
            # Compose hasn't finished yet — first ``on_mount`` will catch up.
            return
        if self._help_visible:
            hint.update("")
            full.update(self._help_text())
            full.add_class("-visible")
        else:
            hint.update("[bold]h[/]  show help")
            full.remove_class("-visible")

    def _help_text(self) -> str:
        """Compact horizontal row of non-obvious bindings. Surface the global mode/navigation
        keys; per-dialog keys (← / → / enter / esc inside an open dialog) are documented by the
        dialog widgets themselves."""
        rows = [
            ("h", "hide help"),
            ("m", "multi-select"),
            ("space", "toggle row"),
            ("shift+↑/↓", "range-select"),
            ("d / s / f / e", "delete / sort / filter / edit"),
            ("ctrl+f", "linked flashcards"),
            ("l", "relink mode"),
            ("alt+←/→", "focus next/prev region"),
        ]
        return "    ".join(f"[bold]{key}[/]  {label}" for key, label in rows)

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
    # Cross-region focus (driven by ``BrowserView``'s alt+left/right)
    # ------------------------------------------------------------------
    #
    # Two regions at this level: the entries table and the details panel. The details panel has its
    # own internal cycle (title → content → choices) which we delegate to ``EntryDetailsView``. The
    # bool returns let the ``BrowserView`` know when the tab is at its leftmost edge so it can roll
    # focus back to the tree.

    def focus_first(self) -> None:
        """Entry point when ``BrowserView`` enters the tab from the tree. Land on the active
        dialog if there is one (so the user picks up where they left off after a tree side-trip),
        else the entries table."""
        if self._active_dialog is not None:
            try:
                widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
                self.query_one(widget_id, Widget).focus()
                return
            except Exception:
                pass
        self.query_one("#entries-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        linked = self.query_one(LinkedFlashcardsPanelView)
        # The right-hand region depends on state: details in ENTRIES, linked-flashcards table in
        # LINKED_FLASHCARDS.
        right = (
            linked
            if self._vm.state is self._vm.State.LINKED_FLASHCARDS
            else details
        )
        if focused is table:
            # In ``ENTRIES`` + multi-select, the details panel is frozen and has no useful edit
            # affordances — short-circuit so ``alt+right`` keeps the user on the table.
            if (
                self._vm.state is self._vm.State.ENTRIES
                and self._vm.multi_select_active
            ):
                return False
            right.focus_first()
            return True
        if focused is not None and right in focused.ancestors_with_self:
            return right.focus_next_region()
        # Defensive fallback: focus was somewhere unexpected. Start the cycle from the leftmost
        # region.
        self.focus_first()
        return True

    def focus_prev_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        linked = self.query_one(LinkedFlashcardsPanelView)
        right = (
            linked
            if self._vm.state is self._vm.State.LINKED_FLASHCARDS
            else details
        )
        if focused is table:
            # Tab's leftmost edge — let ``BrowserView`` hand focus to the tree.
            return False
        if focused is not None and right in focused.ancestors_with_self:
            moved = right.focus_prev_region()
            if not moved:
                table.focus()
            return True
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
