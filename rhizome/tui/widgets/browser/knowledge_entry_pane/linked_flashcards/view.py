"""LinkedFlashcardsPaneView — flashcard ``DataTable`` + search bar + status row.

Read-only for now (just up/down navigation); see ``view_model.py`` for the VM contract. Visual shape
mirrors ``KnowledgeEntryBrowserPaneView`` so the user gets the same affordances on either side: the
top search box behaves identically, the table colouring matches the entries-table palette in
non-multi-select mode, and the status row sits docked at the bottom with the same "showing N of M"
formatting.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Static

from .view_model import LinkedFlashcardsPaneViewModel


class _LinkedFlashcardsTable(DataTable):
    """``DataTable`` subclass with the auto-load-more-at-bottom behaviour the entries table uses. No
    multi-select, no edit/delete bindings — this iteration is read-only navigation. The cursor still
    needs to round-trip through ``vm.set_cursor`` so the VM's persisted cursor stays in sync across
    refetches (mirrors the entries-side wiring)."""

    def __init__(
        self,
        view_model: LinkedFlashcardsPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge — mirrors ``_EntriesTable.action_cursor_down``.
        ``load_more`` is a no-op when nothing further is available or a fetch is in flight."""
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()


class _LinkedFlashcardsSearchInput(Input):
    """Search box mounted above the flashcards table. Visually + behaviourally identical to the
    entries-side ``_SearchInput`` (transparent background, ``#3a3a3a`` border that flips accent on
    focus, right-aligned border-title hint, esc × 2 to clear).

    Lives as its own widget rather than a shared generic because the existing entries-side one is
    typed against ``KnowledgeEntryBrowserPaneViewModel``; a future refactor could lift both onto a
    small ``HasSetSearch`` protocol but it's not worth the indirection today.
    """

    DEFAULT_CSS = """
    _LinkedFlashcardsSearchInput {
        background: transparent;
        border: solid #3a3a3a;
        height: 3;
        padding: 0 1;
    }
    _LinkedFlashcardsSearchInput:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "handle_escape", show=False),
    ]

    def __init__(
        self,
        view_model: LinkedFlashcardsPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self.armed_for_clear: bool = False
        self.border_title_align = "right"
        self._refresh_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is not self:
            return
        self._vm.set_search(event.value)

    def action_handle_escape(self) -> None:
        if self.armed_for_clear:
            self.value = ""
            self._vm.set_search("")
            self.armed_for_clear = False
        else:
            self.armed_for_clear = True
        self._refresh_title()

    def on_key(self, event) -> None:
        if event.key != "escape" and self.armed_for_clear:
            self.armed_for_clear = False
            self._refresh_title()

    def _refresh_title(self) -> None:
        if self.armed_for_clear:
            self.border_title = "[bold #ff8787]press esc again to clear[/]"
        else:
            self.border_title = "[dim]enter to submit • esc × 2 to clear[/]"


class LinkedFlashcardsPaneView(Vertical):
    """Right-hand companion to the entries table when the parent pane is in
    ``State.LINKED_FLASHCARDS``. Columns: id / question / answer.

    Layout: search bar over the flashcard table, with a docked one-line status row at the bottom
    (matches the entries-pane layout). Question + answer columns are auto-sized but capped so a
    single long card doesn't expand the column to several screens wide; the ``DataTable`` ellipsises
    overflow.
    """

    DEFAULT_CSS = """
    LinkedFlashcardsPaneView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    LinkedFlashcardsPaneView #linked-flashcards-table {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    LinkedFlashcardsPaneView #linked-flashcards-status {
        dock: bottom;
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    """

    # Question / answer columns are capped at 40 chars each — the right pane is 50% wide, so a
    # longer column would force the other to collapse. ``DataTable`` ellipsises anything wider.
    _TEXT_COLUMN_WIDTH = 40

    def __init__(
        self,
        view_model: LinkedFlashcardsPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Row-signature edge detector — same three-path refresh as the entries pane (rebuild /
        # extend / inplace) so cursor + scroll survive non-structural changes. ``None`` forces the
        # first refresh through the rebuild path.
        self._last_row_signature: tuple[int, ...] | None = None

    def compose(self):
        table = _LinkedFlashcardsTable(
            self._vm,
            id="linked-flashcards-table",
            cursor_type="row",
            zebra_stripes=True,
        )
        table.add_column("id")
        table.add_column("question", width=self._TEXT_COLUMN_WIDTH)
        table.add_column("answer", width=self._TEXT_COLUMN_WIDTH)
        yield _LinkedFlashcardsSearchInput(self._vm, id="linked-flashcards-search-input")
        yield table
        yield Static("", id="linked-flashcards-status")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        table = self.query_one("#linked-flashcards-table", DataTable)

        # Three-path refresh — identical strategy to the entries pane. Rebuild on structural change
        # (refetch, entry-id change), extend on append (load_more), in-place on pure style churn.
        # Preserves scroll + cursor on the non-rebuild paths so the user's view doesn't jump.
        new_signature = tuple(fc.id for fc in self._vm.flashcards)
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

        for i in range(start, len(self._vm.flashcards)):
            fc = self._vm.flashcards[i]
            # Zebra-pair colouring matching the entries table in non-multi-select mode: odd rows
            # dim, even rows default fg. Selection styling lives on the entries side; this table
            # has no selection state.
            style = "#a0a0a0" if i % 2 else ""
            cells = (
                Text(str(fc.id), style=style),
                Text(fc.question_text, style=style),
                Text(fc.answer_text, style=style),
            )
            if path == "inplace":
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
            else:
                table.add_row(*cells, key=str(fc.id))
        self._last_row_signature = new_signature

        # Restore the cursor after a rebuild — ``clear()`` reset the table to row 0 (see the
        # entries-pane comment for the round-trip-through-RowHighlighted dance).
        if (
            path == "rebuild"
            and self._vm.flashcards
            and 0 <= self._vm.cursor < len(self._vm.flashcards)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#linked-flashcards-status", Static)
        status.update(self._format_status())

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        if self._vm.entry_id is None:
            # The parent pane only feeds an entry id while in ``LINKED_FLASHCARDS``, so this branch
            # only fires when the entries window is empty (no row to highlight).
            return "no entry highlighted"
        total = self._vm.total
        loaded = len(self._vm.flashcards)
        if total is None:
            return "no flashcards linked" if loaded == 0 else f"{loaded} loaded"
        if loaded < total:
            return f"showing {loaded} of {total}"
        if total == 0:
            return "no flashcards linked"
        if total == 1:
            return "1 linked flashcard"
        return f"{total} linked flashcards"

    # ------------------------------------------------------------------
    # Cross-region focus (driven by the parent pane's alt+left/right)
    # ------------------------------------------------------------------
    #
    # Only one focusable sub-region for now: the table. The search bar is excluded from the
    # alt+left/right walk — same convention as the entries-side search bar — so cycling through
    # this pane is single-stop. If the search bar joins the walk later, extend with a
    # ``_REGION_IDS`` tuple + index helper like ``EntryDetailsView``.

    def focus_first(self) -> None:
        """Land on the leftmost focusable sub-region (the table). Called by the parent pane when
        ``BrowserView`` enters the linked-flashcards region from the entries table."""
        self.query_one("#linked-flashcards-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        # Single region; no further step inside this pane.
        return False

    def focus_prev_region(self) -> bool:
        # Single region; no further step inside this pane.
        return False

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Push the cursor back into the VM. Mirrors the entries-pane handler; the VM early-returns
        when the index is unchanged, so this is safe to fire from programmatic ``move_cursor`` calls
        during ``_refresh``."""
        if event.data_table.id != "linked-flashcards-table":
            return
        self._vm.set_cursor(event.cursor_row)
