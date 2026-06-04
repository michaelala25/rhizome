"""View for the linked-flashcards panel. Search bar / ``DataTable`` / relink Accept-Cancel /
answer preview / status row.

In **non-relink** the table shows the linked flashcards only; the "sel" column renders empty.
In **relink** the layout is ``[*pinned, boundary, *pool]``, the "sel" column carries
``[x]``/``[ ]`` markers, the ``-relink`` class flips on the darker palette, and selected rows
render bright green + bold. The pool paginates via ``vm.load_more``; ``space`` toggles the
cursor row's relink-set membership (no-op on the boundary, gated by the VM).

The relink Accept/Cancel widget reveals between the table and the answer preview when
``vm.is_relink_dirty`` is True, via a CSS class toggle (see ``RelinkMenu`` and ``_refresh``).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from rhizome.app.browser.tabs.entries.linked_flashcards import LinkedFlashcardsPanelVM
from rhizome.tui.widgets.shared.search_bar import SearchBar
from rhizome.tui.widgets.browser.tabs.entries.flashcard_preview import FlashcardPreview
from rhizome.tui.widgets.browser.tabs.entries.linked_flashcards_table import LinkedFlashcardsTable
from rhizome.tui.widgets.browser.tabs.entries.relink import RelinkMenu

# Boundary sentinel for the row-signature tuple. Negative is safe — flashcard ids are positive
# autoincrement ints, so the sentinel can't collide with a real row's signature.
_BOUNDARY_SIG: int = -1

# Boundary row key (string so it can't collide with flashcard-id keys, which are ``str(fc.id)``).
_BOUNDARY_ROW_KEY = "__boundary__"


class LinkedFlashcardsPanel(Vertical):
    """Right-hand panel for ``State.LINKED_FLASHCARDS``. Columns: sel / id / question / answer.
    Layout: search → table → relink Accept/Cancel (toggled by CSS) → answer preview → docked
    status row."""

    DEFAULT_CSS = """
    LinkedFlashcardsPanel {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    LinkedFlashcardsPanel #linked-flashcards-table {
        width: 1fr;
        height: 2fr;
        margin: 1 0 0 0;
    }
    LinkedFlashcardsPanel #linked-flashcards-answer-preview {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    LinkedFlashcardsPanel #linked-flashcards-status {
        dock: bottom;
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    /* Relink wash — darker so the selected rows pop. Mirrors the entries table's
       ``-multi-select`` class. */
    LinkedFlashcardsPanel #linked-flashcards-table.-relink {
        background: $surface-darken-2;
    }
    LinkedFlashcardsPanel #linked-flashcards-table.-relink > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    /* Relink Accept/Cancel — visibility driven by ``.-visible`` class on dirty/clean transition.
       Thin top border that flips accent on focus (mirrors the entry-details choices). */
    LinkedFlashcardsPanel #linked-flashcards-relink-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    LinkedFlashcardsPanel #linked-flashcards-relink-choices.-visible {
        display: block;
    }
    LinkedFlashcardsPanel #linked-flashcards-relink-choices:focus {
        border-top: solid $accent;
    }
    """

    # Cap question / answer columns at 40 chars; the right panel is 50% of the tab width and a
    # wider column would force the other to collapse. ``DataTable`` ellipsises overflow.
    _TEXT_COLUMN_WIDTH = 40

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelVM,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Row-signature edge detector — three-path refresh (rebuild / extend / inplace) so the
        # cursor + scroll survive non-structural changes. Boundary appears in the signature as
        # ``_BOUNDARY_SIG`` so relink mode flips force a rebuild.
        self._last_row_signature: tuple[int, ...] | None = None
        # Tracked across refreshes for focus-orphan rescue: dirty→clean hides the choices widget
        # out from under any focus that was on it.
        self._last_relink_dirty: bool = False

    def compose(self):
        table = LinkedFlashcardsTable(
            self._vm,
            id="linked-flashcards-table",
            cursor_type="row",
            zebra_stripes=True,
        )
        # "sel" column always present (DataTable can't drop columns cleanly). Empty outside
        # relink; ``[ ]`` / ``[x]`` markers in relink (boundary row stays blank).
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("question", width=self._TEXT_COLUMN_WIDTH)
        table.add_column("answer", width=self._TEXT_COLUMN_WIDTH)
        yield SearchBar[LinkedFlashcardsPanelVM](
            self._vm, id="linked-flashcards-search-input",
        )
        yield table
        # Mounted unconditionally so its VM subscription survives across show/hide cycles;
        # visibility flipped by the ``.-visible`` CSS class in ``_refresh``.
        yield RelinkMenu(self._vm, id="linked-flashcards-relink-choices")
        yield FlashcardPreview(self._vm, id="linked-flashcards-answer-preview")
        yield Static("", id="linked-flashcards-status")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        table = self.query_one("#linked-flashcards-table", DataTable)
        relink = self._vm.relink_mode
        table.set_class(relink, "-relink")

        # Row signature: linked ids, then (in relink) boundary + remaining ids. Drives the
        # three-path refresh — any structural change forces rebuild, ``load_more`` is a prefix
        # match → extend, and a pure relink toggle is signature-equal → inplace cell updates.
        linked_sig = tuple(fc.id for fc in self._vm.linked_flashcards)
        if relink:
            new_signature: tuple[int, ...] = (
                linked_sig
                + (_BOUNDARY_SIG,)
                + tuple(fc.id for fc in self._vm.remaining_flashcards)
            )
        else:
            new_signature = linked_sig

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

        n_linked = len(self._vm.linked_flashcards)
        boundary_idx = n_linked if relink else -1  # only used when relink is True

        for i in range(start, len(new_signature)):
            row_sig = new_signature[i]
            if relink and i == boundary_idx:
                cells = self._render_boundary_row()
                row_key = _BOUNDARY_ROW_KEY
            else:
                if relink and i > boundary_idx:
                    fc = self._vm.remaining_flashcards[i - boundary_idx - 1]
                else:
                    fc = self._vm.linked_flashcards[i]
                cells = self._render_flashcard_row(
                    fc, zebra_index=i, relink=relink,
                )
                row_key = str(row_sig)
            if path == "inplace":
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
            else:
                table.add_row(*cells, key=row_key)

        self._last_row_signature = new_signature

        # Restore cursor after a rebuild (``clear()`` resets to row 0). ``move_cursor`` fires
        # ``RowHighlighted`` → ``vm.set_cursor``; the VM's equality early-return breaks the loop.
        if (
            path == "rebuild"
            and len(new_signature) > 0
            and 0 <= self._vm.cursor < len(new_signature)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#linked-flashcards-status", Static)
        status.update(self._format_status())

        # Relink Accept/Cancel visibility. Class toggle (not unmount) so the VM subscription
        # survives across show/hide cycles.
        choices = self.query_one("#linked-flashcards-relink-choices", RelinkMenu)
        dirty_now = self._vm.is_relink_dirty
        was_dirty = self._last_relink_dirty
        choices.set_class(dirty_now, "-visible")
        # Focus-orphan rescue: on dirty→clean the choices widget is hidden out from under any
        # focus that was on it; ``screen.focused`` would then point at a ``display: none`` widget
        # and the next keystroke would go nowhere visible. Re-route to the table.
        if was_dirty and not dirty_now and self.screen is not None:
            if self.screen.focused is choices:
                try:
                    self.query_one("#linked-flashcards-table", DataTable).focus()
                except Exception:
                    pass
        self._last_relink_dirty = dirty_now

    def _render_flashcard_row(
        self, fc, *, zebra_index: int, relink: bool,
    ) -> tuple[Text, Text, Text, Text]:
        """Build the four cells for a flashcard row. Three colour regimes (mirrors entries):
        non-relink zebra; relink-unselected darker zebra; relink-selected bold green."""
        selected = relink and fc.id in self._vm.relink_selected_ids
        if selected:
            style = "bold #5fd75f"
        elif relink:
            style = "#787878" if zebra_index % 2 else "#a0a0a0"
        else:
            style = "#a0a0a0" if zebra_index % 2 else ""
        marker = ("[x]" if selected else "[ ]") if relink else ""
        return (
            Text(marker, style=style),
            Text(str(fc.id), style=style),
            Text(fc.question_text, style=style),
            Text(fc.answer_text, style=style),
        )

    def _render_boundary_row(self) -> tuple[Text, Text, Text, Text]:
        """The pinned/pool divider. Dim ``─`` glyphs so the row reads as a separator. Cursor
        lands here but ``toggle_current_relink_selection`` no-ops (VM guards on cursor section)."""
        sep_style = "#3a3a3a"
        return (
            Text("───", style=sep_style),
            Text("──", style=sep_style),
            Text("─" * self._TEXT_COLUMN_WIDTH, style=sep_style),
            Text("─" * self._TEXT_COLUMN_WIDTH, style=sep_style),
        )

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        if self._vm.relink_mode:
            # Two-number framing: selected (what would link on commit) + pool size.
            count = len(self._vm.relink_selected_ids)
            noun = "flashcard" if count == 1 else "flashcards"
            pool = self._vm.remaining_total
            pool_loaded = len(self._vm.remaining_flashcards)
            if pool is None:
                return f"relink: {count} {noun} selected (l to exit, space to toggle)"
            if pool_loaded < pool:
                return (
                    f"relink: {count} {noun} selected · pool {pool_loaded}/{pool} "
                    "(l to exit, space to toggle)"
                )
            return (
                f"relink: {count} {noun} selected · pool {pool} "
                "(l to exit, space to toggle)"
            )
        n_entries = len(self._vm.entry_ids)
        if n_entries == 0:
            return "no entries selected"
        loaded = len(self._vm.linked_flashcards)
        prefix = f"{n_entries} entries · " if n_entries > 1 else ""
        if loaded == 0:
            return f"{prefix}no flashcards linked"
        if loaded == 1:
            return f"{prefix}1 linked flashcard"
        return f"{prefix}{loaded} linked flashcards"

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(DataTable.RowHighlighted)
    def _on_linked_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Push the cursor back into the VM. VM's equality early-return is what breaks the loop
        with the programmatic ``move_cursor`` in ``_refresh``."""
        if event.data_table.id != "linked-flashcards-table":
            return
        self._vm.set_cursor(event.cursor_row)
