from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalModel
from rhizome.tui.keybindings import Keybind

from .messages import SetTopicRequested


class FlashcardList(DataTable):
    """Three-column DataTable backed by ``vm.flashcards``. Cursor lives in the table; we only
    forward keypresses to the VM. Subscribes to ``OnFlashcardsChanged`` for per-row repaint."""

    can_focus = True

    DEFAULT_CSS = """
    FlashcardList {
        width: 1fr;
        height: auto;
        min-height: 5;
        max-height: 20;
    }
    """

    BINDINGS = [
        Keybind.ProposalToggleExclude.as_binding("toggle_exclude", show=False),
        Keybind.ProposalSetTopic.     as_binding("set_topic",      show=False),
    ]

    _ANSWER_MIN_WIDTH = 15

    def __init__(self, vm: FlashcardProposalModel, **kwargs: Any) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            zebra_stripes=True,
            cursor_type="row",
            **kwargs,
        )
        self.model = vm
        self._answer_key = None

    def on_mount(self) -> None:
        self.add_columns("Question", "Topic")
        self._answer_key = self.add_column("Answer", width=self._ANSWER_MIN_WIDTH)

        for _ in self.model.flashcards:
            self.add_row("", "", "")

        self.model.subscribe(self.model.Callbacks.OnFlashcardsChanged, self._on_flashcards_changed)

        for i in range(len(self.model.flashcards)):
            self._refresh_row(i)

    def on_unmount(self) -> None:
        self.model.unsubscribe(self.model.Callbacks.OnFlashcardsChanged, self._on_flashcards_changed)

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
        target = max(self._ANSWER_MIN_WIDTH, self.size.width - others - 2 * self.cell_padding)

        if answer_col.width != target:
            answer_col.width = target
            self.refresh(layout=True)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "cursor_up":
            return self.cursor_row > 0
        if action == "cursor_down":
            return self.cursor_row < self.row_count - 1
        if action == "select_cursor":
            return False  # bubble enter to the parent's collapse toggle
        return True

    def action_toggle_exclude(self) -> None:
        if self.model.is_done:
            return
        self.model.toggle_excluded(self.cursor_row)

    def action_set_topic(self) -> None:
        if self.model.is_done:
            return
        self.post_message(SetTopicRequested(scope="current"))

    def _on_flashcards_changed(self, indices: list[int]) -> None:
        for idx in indices:
            if 0 <= idx < self.row_count:
                self._refresh_row(idx)

    def _refresh_row(self, idx: int) -> None:
        flashcard = self.model.flashcards[idx]
        excluded = self.model.is_excluded(idx)
        style = "dim strike" if excluded else ""

        question_preview = " ".join((flashcard.question or "").split()) or "(empty)"
        question = Text(question_preview, style=style)
        topic = Text(flashcard.topic.name if flashcard.topic else "(none)", style=style or "dim")

        answer_preview = " ".join((flashcard.answer or "").split()) or "(empty)"
        answer = Text(answer_preview, style=style or "dim")

        self.update_cell_at(Coordinate(idx, 0), question)
        self.update_cell_at(Coordinate(idx, 1), topic)
        self.update_cell_at(Coordinate(idx, 2), answer)
