"""LinkedFlashcardsPanelView — flashcard ``DataTable`` + search bar + status row.

Read-only for now (just up/down navigation); see ``view_model.py`` for the VM contract. Visual shape
mirrors ``KnowledgeEntryBrowserTabView`` so the user gets the same affordances on either side: the
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
from textual.widgets import DataTable, Input, Static, TextArea

from .view_model import LinkedFlashcardsPanelViewModel


class _LinkedFlashcardsTable(DataTable):
    """``DataTable`` subclass with the auto-load-more-at-bottom behaviour the entries table uses. No
    multi-select, no edit/delete bindings — this iteration is read-only navigation. The cursor still
    needs to round-trip through ``vm.set_cursor`` so the VM's persisted cursor stays in sync across
    refetches (mirrors the entries-side wiring)."""

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelViewModel,
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
    typed against ``KnowledgeEntryBrowserTabViewModel``; a future refactor could lift both onto a
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
        view_model: LinkedFlashcardsPanelViewModel,
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


class _FlashcardAnswerPreview(TextArea):
    """Read-only scrollable preview of the cursor flashcard's answer + testing notes. Non-navigable
    (``can_focus=False``) — mouse-wheel scroll works, keyboard nav doesn't land here.

    Subscribes to the VM's ``dirty``, which now also fires on cursor moves. Re-reads the cursor
    flashcard on each fire and rebuilds the text.
    """

    can_focus = False

    DEFAULT_CSS = """
    _FlashcardAnswerPreview {
        background: transparent;
        border: solid #3a3a3a;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            read_only=True, show_line_numbers=False, soft_wrap=True, **kwargs,
        )
        self._vm = view_model
        self.border_title = "[dim]answer + notes[/]"
        self.border_title_align = "left"

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        fc = self._vm.cursor_flashcard
        if fc is None:
            target = ""
        else:
            parts = ["Answer:", fc.answer_text]
            if fc.testing_notes:
                # Trailing whitespace on the existing answer would push the notes header onto a
                # weird line — strip before joining.
                parts.extend(["", "Testing notes:", fc.testing_notes])
            target = "\n".join(parts)
        # Guard the assignment so we don't trigger a TextArea.Changed event for a no-op rewrite
        # (read_only suppresses user edits, not programmatic ones).
        if self.text != target:
            self.text = target


class LinkedFlashcardsPanelView(Vertical):
    """Right-hand companion to the entries table when the parent tab is in
    ``State.LINKED_FLASHCARDS``. Columns: id / question / answer.

    Layout: search bar over the flashcard table, with a docked one-line status row at the bottom
    (matches the entries-tab layout). Question + answer columns are auto-sized but capped so a
    single long card doesn't expand the column to several screens wide; the ``DataTable`` ellipsises
    overflow.
    """

    DEFAULT_CSS = """
    LinkedFlashcardsPanelView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    /* Table + preview split the available column 2:1 vertically — table gets the larger share,
       preview gets enough room (~1/3) to show a few lines of answer + notes without dwarfing the
       table. The status row docks to the bottom and doesn't participate in the fr split. */
    LinkedFlashcardsPanelView #linked-flashcards-table {
        width: 1fr;
        height: 2fr;
        margin: 1 0 0 0;
    }
    LinkedFlashcardsPanelView #linked-flashcards-answer-preview {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    LinkedFlashcardsPanelView #linked-flashcards-status {
        dock: bottom;
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    """

    # Question / answer columns are capped at 40 chars each — the right tab is 50% wide, so a
    # longer column would force the other to collapse. ``DataTable`` ellipsises anything wider.
    _TEXT_COLUMN_WIDTH = 40

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Row-signature edge detector — same three-path refresh as the entries tab (rebuild /
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
        yield _FlashcardAnswerPreview(self._vm, id="linked-flashcards-answer-preview")
        yield Static("", id="linked-flashcards-status")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        table = self.query_one("#linked-flashcards-table", DataTable)

        # Three-path refresh — identical strategy to the entries tab. Rebuild on structural change
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
        # entries-tab comment for the round-trip-through-RowHighlighted dance).
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
        n_entries = len(self._vm.entry_ids)
        if n_entries == 0:
            # Empty set — either the entries window is empty (no cursor to read from), or the user
            # is in multi-select with nothing selected. Both surface as "no entries selected" since
            # the panel is showing the same empty state in either case.
            return "no entries selected"
        total = self._vm.total
        loaded = len(self._vm.flashcards)
        # In the multi-entry case lead with the selection count so the user can see how many
        # entries the union is over without looking elsewhere.
        prefix = f"{n_entries} entries · " if n_entries > 1 else ""
        if total is None:
            return f"{prefix}no flashcards linked" if loaded == 0 else f"{prefix}{loaded} loaded"
        if loaded < total:
            return f"{prefix}showing {loaded} of {total}"
        if total == 0:
            return f"{prefix}no flashcards linked"
        if total == 1:
            return f"{prefix}1 linked flashcard"
        return f"{prefix}{total} linked flashcards"

    # ------------------------------------------------------------------
    # Cross-region focus (driven by the parent tab's alt+left/right)
    # ------------------------------------------------------------------
    #
    # Only one focusable sub-region for now: the table. The search bar is excluded from the
    # alt+left/right walk — same convention as the entries-side search bar — so cycling through
    # this tab is single-stop. If the search bar joins the walk later, extend with a
    # ``_REGION_IDS`` tuple + index helper like ``EntryDetailsView``.

    def focus_first(self) -> None:
        """Land on the leftmost focusable sub-region (the table). Called by the parent tab when
        ``BrowserView`` enters the linked-flashcards region from the entries table."""
        self.query_one("#linked-flashcards-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        # Single region; no further step inside this tab.
        return False

    def focus_prev_region(self) -> bool:
        # Single region; no further step inside this tab.
        return False

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Push the cursor back into the VM. Mirrors the entries-tab handler; the VM early-returns
        when the index is unchanged, so this is safe to fire from programmatic ``move_cursor`` calls
        during ``_refresh``."""
        if event.data_table.id != "linked-flashcards-table":
            return
        self._vm.set_cursor(event.cursor_row)
