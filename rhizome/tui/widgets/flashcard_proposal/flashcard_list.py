"""``FlashcardList`` — focusable ``DataTable`` of pending flashcards.

Three columns: Question, Topic, Answer. ``Answer`` is sized dynamically each resize to absorb the
remaining horizontal budget, mirroring the ``Content`` column in
``rhizome.tui.widgets.commit_proposal.entry_list.EntryList``. The row set is fixed at mount (the
proposal can't grow or shrink its flashcard list — ``reset()`` re-clones in place), so rows are
added once and only their cell contents are mutated thereafter via ``update_cell_at``. That
side-steps the spurious ``RowHighlighted`` events ``clear`` + ``add_row`` would otherwise post.

Cursor movement is driven by ``DataTable``'s default ``cursor_up`` / ``cursor_down`` actions; the
resulting ``RowHighlighted`` is forwarded to ``vm.set_cursor``. ``check_action`` disables the
binding at the top/bottom row so the keystroke bubbles to the parent ``FlashcardProposal``'s
focus-graph bindings.

Excluded rows render dim + strikethrough across all three columns.

Subscribes to both ``vm.dirty`` and ``vm.details.dirty`` — the latter so cell content re-renders
when an in-place question/answer/notes edit is accepted on the focused flashcard.

See the EntryList module docstring for the fast-key-repeat feedback-loop rationale behind
``_handling_row_highlighted`` and the lru_cache monkey-patch on ``_get_styles_to_render_cell``.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalVM
from rhizome.tui.widgets.flashcard_proposal.messages import SetTopicRequested


class FlashcardList(DataTable, can_focus=True):

    DEFAULT_CSS = """
    FlashcardList {
        width: 1fr;
        height: auto;
        min-height: 5;
        max-height: 20;
    }
    """

    BINDINGS = [
        Binding("d", "toggle_exclude", show=False),
        Binding("t", "set_topic", show=False),
        # ``up`` / ``down`` use the inherited ``DataTable`` bindings — see module docstring for
        # the cursor-routing rationale. ``e`` is not bound locally either; it bubbles to the
        # parent FlashcardProposal's "e" binding, which forwards focus into the details panel.
    ]

    _ANSWER_MIN_WIDTH = 15

    def __init__(self, vm: FlashcardProposalVM, **kwargs) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            zebra_stripes=True,
            cursor_type="row",
            **kwargs,
        )
        self._vm = vm
        self._answer_key = None
        # True for the duration of ``on_data_table_row_highlighted``. Suppresses the cursor-sync
        # ``move_cursor`` in ``_refresh`` while the table is the source of truth.
        self._handling_row_highlighted: bool = False

    def on_mount(self) -> None:
        self.add_columns("Question", "Topic")
        # Answer column starts at the minimum and is widened in ``_fit_answer_column`` to absorb
        # whatever horizontal space the auto-sized columns leave behind — keeps long answer text
        # from inducing a horizontal scrollbar.
        self._answer_key = self.add_column("Answer", width=self._ANSWER_MIN_WIDTH)
        # One-time population. Cell contents are filled in by the initial ``_refresh`` below and
        # by every subsequent ``vm.dirty`` emit, exclusively through ``update_cell_at`` — no
        # ``clear`` / ``add_row`` ever runs again on this table.
        for i in range(len(self._vm.flashcards)):
            self.add_row("", "", "", key=str(i))
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.details.subscribe(self._vm.details.dirty, self._refresh)
        self._refresh()

    def on_resize(self) -> None:
        self._fit_answer_column()

    def _fit_answer_column(self) -> None:
        if self._answer_key is None or self.size.width <= 0:
            return
        answer_col = self.columns.get(self._answer_key)
        if answer_col is None:
            return
        others = sum(
            c.get_render_width(self) for k, c in self.columns.items() if k != self._answer_key
        )
        available = self.size.width - others - 2 * self.cell_padding
        target = max(self._ANSWER_MIN_WIDTH, available)
        if answer_col.width != target:
            answer_col.width = target
            self.refresh(layout=True)

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.details.unsubscribe(self._vm.details.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Boundary detection — disable cursor_up at the top row and cursor_down at the bottom so the
    # key bubbles up to the parent's own up/down handler.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "cursor_up":
            return self._vm.cursor is not None and self._vm.cursor > 0
        if action == "cursor_down":
            return (
                self._vm.cursor is not None
                and self._vm.cursor < len(self._vm.flashcards) - 1
            )
        return True

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_toggle_exclude(self) -> None:
        self._vm.toggle_exclude_current_flashcard()

    def action_set_topic(self) -> None:
        self.post_message(SetTopicRequested(scope="current"))

    # ------------------------------------------------------------------
    # DataTable's default cursor actions move the row cursor and post ``RowHighlighted``; this
    # handler pushes the new index into the VM. The VM's equality guard on ``set_cursor`` absorbs
    # the bounce when the move was VM-initiated.
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._handling_row_highlighted = True
        try:
            self._vm.set_cursor(event.cursor_row)
        finally:
            self._handling_row_highlighted = False

    # ------------------------------------------------------------------
    # Per-row cursor tint — DataTable's component-class lookup is flat per cell, so we hook
    # ``_get_styles_to_render_cell`` (bypassing its lru_cache, which doesn't key on row) and
    # paint a different bg when the cursor lands on an excluded flashcard.
    # ------------------------------------------------------------------

    _EXCLUDED_CURSOR_STYLE = Style(bgcolor="rgb(28,52,86)")

    def _get_styles_to_render_cell(
        self,
        is_header_cell: bool,
        is_row_label_cell: bool,
        is_fixed_style_cell: bool,
        hover: bool,
        cursor: bool,
        show_cursor: bool,
        show_hover_cursor: bool,
        has_css_foreground_priority: bool,
        has_css_background_priority: bool,
    ) -> tuple[Style, Style]:
        component_style, post_style = super()._get_styles_to_render_cell(
            is_header_cell, is_row_label_cell, is_fixed_style_cell,
            hover, cursor, show_cursor, show_hover_cursor,
            has_css_foreground_priority, has_css_background_priority,
        )
        if cursor and show_cursor and not is_header_cell and not is_row_label_cell:
            row = self.cursor_row
            if 0 <= row < len(self._vm.flashcards) and self._vm.is_excluded(row):
                component_style += self._EXCLUDED_CURSOR_STYLE
                if has_css_background_priority:
                    post_style += self._EXCLUDED_CURSOR_STYLE
        return component_style, post_style

    # See EntryList for the rationale; the override deliberately skips caching so the per-row
    # tint can update, but DataTable._clear_caches still calls .cache_clear() on the descriptor.
    _get_styles_to_render_cell.cache_clear = lambda: None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        cursor = self._vm.cursor
        for i, flashcard in enumerate(self._vm.flashcards):
            is_excluded = self._vm.is_excluded(i)
            style = "dim strike" if is_excluded else ""

            question_preview = " ".join((flashcard.question or "").split()) or "(empty)"
            question = Text(question_preview, style=style)
            topic = Text(flashcard.topic_name or "(none)", style=style or "dim")
            # Collapse newlines so multi-line content stays on one row; the cell itself will
            # truncate per the column's width budget.
            answer_preview = " ".join((flashcard.answer or "").split()) or "(empty)"
            answer = Text(answer_preview, style=style or "dim")

            self.update_cell_at(Coordinate(i, 0), question)
            self.update_cell_at(Coordinate(i, 1), topic)
            self.update_cell_at(Coordinate(i, 2), answer)

        # Sync the table's visual cursor to vm.cursor for VM-initiated moves (boundary navigation
        # from the parent view, ``reset()``'s clamp). Suppressed inside ``RowHighlighted``
        # handling because the table is the source of truth there — see EntryList docstring.
        if (
            not self._handling_row_highlighted
            and cursor is not None
            and self.cursor_row != cursor
        ):
            self.move_cursor(row=cursor, animate=False)

        self._fit_answer_column()
