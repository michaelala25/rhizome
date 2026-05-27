"""LinkedFlashcardsPanelView — flashcard ``DataTable`` + search bar + status row, rendering one
section in non-relink mode and two sections separated by a visual boundary in relink mode.

In **non-relink**, the table shows just the flashcards linked to the current entry-id set —
the classic cursor-driven panel. The "sel" column renders empty.

In **relink**, the table layout is:

    [pinned linked row 0]
    [pinned linked row 1]
    ...
    [pinned linked row N-1]
    [─ boundary row ─]            ← lands but no-op on toggle
    [remaining pool row 0]
    [remaining pool row 1]
    ...

The "sel" column carries [x]/[ ] markers, the entries-side darkened palette flips on via the
``-relink`` class, and selected rows render bright green + bold. The remaining pool paginates via
``load_more``; ``space`` toggles the cursor row's relink-set membership (no-op on boundary).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Static, TextArea

from .view_model import LinkedFlashcardsPanelViewModel

# Sentinel slipped into the row-signature tuple at the boundary position so signature comparisons
# can distinguish "structurally identical" from "boundary moved" (e.g. linked count changed).
# Negative is safe — flashcard ids are positive autoincrement ints.
_BOUNDARY_SIG: int = -1

# Row key for the boundary row in the DataTable. String to disambiguate from numeric flashcard
# id keys (which are also strings via ``str(fc.id)``).
_BOUNDARY_ROW_KEY = "__boundary__"


class _LinkedFlashcardsTable(DataTable):
    """``DataTable`` subclass with auto-load-more-at-bottom (drives the relink-mode pool
    pagination) and a ``space`` binding for relink toggle. The cursor still needs to round-trip
    through ``vm.set_cursor`` so the VM's persisted cursor stays in sync across refetches
    (mirrors the entries-side wiring)."""

    BINDINGS = [
        # Mirrors the ``space`` binding on ``_EntriesTable``. VM no-ops outside relink, on the
        # boundary row, or when the display is empty.
        Binding("space", "toggle_relink_selection", show=False),
    ]

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge. Relink-mode pool paginates via
        ``vm.load_more``; it's a no-op outside relink, when nothing further is available, or when
        a fetch is in flight, so this is safe to call unconditionally at the bottom edge."""
        if (
            self._vm.remaining_has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_toggle_relink_selection(self) -> None:
        self._vm.toggle_current_relink_selection()


class _LinkedFlashcardsSearchInput(Input):
    """Search box mounted above the flashcards table. Visually + behaviourally identical to the
    entries-side ``_SearchInput`` (transparent background, ``#3a3a3a`` border that flips accent
    on focus, right-aligned border-title hint, esc × 2 to clear).

    In relink mode the search filters the remaining pool only — the pinned section stays
    unconditionally visible. The bar itself doesn't need to know; the VM handles the split.
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
    """Read-only scrollable preview of the cursor flashcard's answer + testing notes. Non-
    navigable (``can_focus=False``) — mouse-wheel scroll works, keyboard nav doesn't land here.

    Subscribes to the VM's ``dirty`` (which fires on cursor moves, refetches, and toggles). Reads
    ``cursor_flashcard`` on each fire — ``None`` when on the boundary row or display is empty.
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
                parts.extend(["", "Testing notes:", fc.testing_notes])
            target = "\n".join(parts)
        if self.text != target:
            self.text = target


class _RelinkChoicesList(Static, can_focus=True):
    """Accept / Cancel choices for committing or discarding a relink edit.

    Visible only when ``vm.is_relink_dirty`` is True; hidden via the ``.-visible`` CSS class
    toggle managed by the parent panel's ``_refresh``. Cursor is owned by the VM
    (``relink_choice_cursor``) so the highlighted choice survives repaints. Mirrors the
    Accept/Cancel choices list on the entry details panel — same focusable-static pattern, just
    horizontal because there are only two options.
    """

    BINDINGS = [
        Binding("left", "choice_left", show=False),
        Binding("right", "choice_right", show=False),
        Binding("enter", "choice_confirm", show=False),
        # Escape acts as Cancel — matches the "easy out" convention from the entry details
        # choices list. Doesn't depend on cursor position.
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelViewModel,
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
        # Cursor brightness tracks focus — mirrors ``_DeleteConfirm`` on the entries side.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        cursor_idx = self._vm.relink_choice_cursor
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        text.append("Relink: ", style="dim")
        labels = ("Accept", "Cancel")
        for i, label in enumerate(labels):
            chosen = i == cursor_idx
            if chosen:
                text.append("► ", style=cursor_color)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
            if i < len(labels) - 1:
                text.append("   ")
        text.append("\n")
        text.append(
            "← / → move • enter confirm • esc cancels", style="dim",
        )
        return text

    def action_choice_left(self) -> None:
        self._vm.move_relink_choice_cursor(-1)

    def action_choice_right(self) -> None:
        self._vm.move_relink_choice_cursor(1)

    async def action_choice_confirm(self) -> None:
        if self._vm.relink_choice_cursor == 0:
            await self._vm.accept_relink()
        else:
            self._vm.cancel_relink()

    def action_cancel(self) -> None:
        self._vm.cancel_relink()


class LinkedFlashcardsPanelView(Vertical):
    """Right-hand companion to the entries table when the parent tab is in
    ``State.LINKED_FLASHCARDS``. Columns: sel / id / question / answer.

    Layout: search bar over the flashcard table, an answer preview below, and a docked one-line
    status row at the bottom (matches the entries-tab layout). In relink mode a boundary row
    separates the pinned linked flashcards from the paginated remaining pool.
    """

    DEFAULT_CSS = """
    LinkedFlashcardsPanelView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
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
    /* Relink wash: shifted darker so the relink pinned/selected rows pop. Mirrors the entries-
       table ``-multi-select`` class. */
    LinkedFlashcardsPanelView #linked-flashcards-table.-relink {
        background: $surface-darken-2;
    }
    LinkedFlashcardsPanelView #linked-flashcards-table.-relink > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    /* Relink Accept/Cancel choices — mounted between the table and the answer preview, visible
       only when ``vm.is_relink_dirty`` is True (``.-visible`` class managed by ``_refresh``).
       Mirrors the entry-details choices styling: thin top border that flips accent on focus. */
    LinkedFlashcardsPanelView #linked-flashcards-relink-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    LinkedFlashcardsPanelView #linked-flashcards-relink-choices.-visible {
        display: block;
    }
    LinkedFlashcardsPanelView #linked-flashcards-relink-choices:focus {
        border-top: solid $accent;
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
        # extend / inplace) so cursor + scroll survive non-structural changes. ``None`` forces
        # the first refresh through the rebuild path. The boundary row appears in the signature
        # as ``_BOUNDARY_SIG`` so a transition into/out of relink (changing display shape)
        # forces a rebuild.
        self._last_row_signature: tuple[int, ...] | None = None
        # Tracks the relink-dirty state at the last refresh so a dirty→clean transition can
        # trigger focus-orphan rescue (the choices widget would otherwise be left focused while
        # hidden). False at boot, since the widget isn't visible until the user toggles
        # something off-baseline.
        self._last_relink_dirty: bool = False

    def compose(self):
        table = _LinkedFlashcardsTable(
            self._vm,
            id="linked-flashcards-table",
            cursor_type="row",
            zebra_stripes=True,
        )
        # Leading "sel" column always present (we can't add or drop columns cleanly after
        # construction). Rendered empty outside relink; in relink each row shows [ ] or [x]
        # (boundary row stays blank). Mirrors the entries-table pattern.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("question", width=self._TEXT_COLUMN_WIDTH)
        table.add_column("answer", width=self._TEXT_COLUMN_WIDTH)
        yield _LinkedFlashcardsSearchInput(self._vm, id="linked-flashcards-search-input")
        yield table
        # Mounted unconditionally; visibility is driven by ``vm.is_relink_dirty`` via the
        # ``.-visible`` class so the widget can subscribe to the VM at mount time and survive
        # multiple dirty→clean→dirty cycles without re-mounting.
        yield _RelinkChoicesList(self._vm, id="linked-flashcards-relink-choices")
        yield _FlashcardAnswerPreview(self._vm, id="linked-flashcards-answer-preview")
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

        # Build the row signature for change detection. Linked ids first; in relink, append the
        # boundary sentinel and the remaining ids. Any change to either section's ids — including
        # the linked-set reshuffling on entry-cursor move — forces a rebuild via signature
        # mismatch; ``load_more`` shows up as a prefix-match → extend; pure style churn (relink
        # toggle landing on selection) → inplace.
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
                # Resolve which section this index belongs to and pick the right flashcard.
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

        # Restore the cursor after a rebuild — ``clear()`` reset the table to row 0.
        # ``move_cursor`` round-trips via ``RowHighlighted`` → ``vm.set_cursor``; the equality
        # early-return there breaks the loop.
        if (
            path == "rebuild"
            and len(new_signature) > 0
            and 0 <= self._vm.cursor < len(new_signature)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#linked-flashcards-status", Static)
        status.update(self._format_status())

        # Relink Accept/Cancel choices visibility. Toggling via a CSS class avoids re-mounting
        # the widget on every dirty→clean→dirty cycle (preserves its VM subscription).
        choices = self.query_one("#linked-flashcards-relink-choices", _RelinkChoicesList)
        dirty_now = self._vm.is_relink_dirty
        was_dirty = self._last_relink_dirty
        choices.set_class(dirty_now, "-visible")
        # Focus-orphan rescue: if the choices widget was just hidden out from under a focused
        # state, ``screen.focused`` is now pointing at a ``display: none`` widget and the next
        # keystroke goes nowhere visible. Re-route focus to the table so the user can keep
        # navigating. Mirrors the entry-details rescue.
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
        """Build the four cells for a real flashcard row.

        Three colour regimes (mirrors the entries table):
          * non-relink: zebra-pair (odd rows dim, even default).
          * relink, not selected: darker zebra pair so the selected rows stand out.
          * relink, selected: bold green to pop against the dimmed sea.
        """
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
        """The visual partition between pinned linked rows and the remaining pool. All cells use
        a dim separator glyph so the row reads as "this is a divider, not a row you'd act on".
        Cursor lands here but ``toggle_current_relink_selection`` no-ops (VM-side guard)."""
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
            # Selection count under the current relink state. The two-number framing (selected /
            # remaining-pool-total) tells the user both "what would link if I committed" and
            # "how big the pool is". Boundary-row landing is silent here — no point telling the
            # user "you're on the boundary".
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

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Push the cursor back into the VM. Mirrors the entries-tab handler; the VM
        early-returns when the index is unchanged, so this is safe to fire from programmatic
        ``move_cursor`` calls during ``_refresh``."""
        if event.data_table.id != "linked-flashcards-table":
            return
        self._vm.set_cursor(event.cursor_row)
