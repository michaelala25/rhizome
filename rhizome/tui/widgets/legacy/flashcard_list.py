"""FlashcardList — read-only widget for browsing flashcards."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rhizome.db import Flashcard

# Color constants (matching EntryList palette)
_DIM = "rgb(100,100,100)"
_EPHEMERAL_DIM = "rgb(80,80,80)"
_FOCUS_GREEN = "rgb(100,200,100)"


class FlashcardList(Widget, can_focus=True):
    """Read-only flashcard list with detail panel for browsing Flashcard objects."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "dismiss", show=False),
        Binding("escape", "dismiss", show=False),
    ]

    DEFAULT_CSS = """
    FlashcardList {
        height: auto;
        layout: vertical;
        padding: 0 1;
    }
    FlashcardList #fv-list-scroll {
        height: auto;
        max-height: 10;
        margin: 1 0 1 0;
    }
    FlashcardList #fv-list {
        height: auto;
    }
    FlashcardList #fv-detail-panel {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    FlashcardList #fv-question-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin-bottom: 0;
    }
    FlashcardList #fv-question {
        margin: 0 0 1 0;
    }
    FlashcardList #fv-answer-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin-bottom: 0;
    }
    FlashcardList #fv-answer-scroll {
        height: auto;
        max-height: 10;
    }
    FlashcardList #fv-answer {
        height: auto;
    }
    FlashcardList #fv-meta {
        color: rgb(100,100,100);
        margin: 1 0 0 0;
    }
    FlashcardList #fv-notes-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin: 1 0 0 0;
    }
    FlashcardList #fv-notes {
        height: auto;
        margin: 0;
    }
    FlashcardList #fv-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 1;
    }
    """

    class Dismissed(Message):
        """Posted when the user presses Escape to leave the flashcard viewer."""

    cursor: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._flashcards: list[Flashcard] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="fv-empty")
        with VerticalScroll(id="fv-list-scroll"):
            yield Static(id="fv-list")
        with Vertical(id="fv-detail-panel"):
            yield Static("Question", id="fv-question-label")
            yield Static(id="fv-question")
            yield Static("Answer", id="fv-answer-label")
            with VerticalScroll(id="fv-answer-scroll"):
                yield Static(id="fv-answer")
            yield Static(id="fv-meta")
            yield Static("Testing notes", id="fv-notes-label")
            yield Static(id="fv-notes")

    def on_mount(self) -> None:
        self._apply_empty_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_flashcards(self, flashcards: list[Flashcard]) -> None:
        """Replace the displayed flashcards and reset the cursor."""
        self._flashcards = list(flashcards)
        self.cursor = 0
        self._apply_empty_state()
        if self._flashcards:
            self._render_list()
            self._render_detail()
            self._scroll_cursor_visible()

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_cursor(self) -> None:
        if self._flashcards:
            self._render_list()
            self._render_detail()
            self._scroll_cursor_visible()

    def _scroll_cursor_visible(self) -> None:
        self.call_after_refresh(self._do_scroll_cursor_visible)

    def _do_scroll_cursor_visible(self) -> None:
        scroll = self.query_one("#fv-list-scroll", VerticalScroll)
        if scroll.size.height == 0:
            return
        if self.cursor < scroll.scroll_y:
            scroll.scroll_y = self.cursor
        elif self.cursor >= scroll.scroll_y + scroll.size.height:
            scroll.scroll_y = self.cursor - scroll.size.height + 1

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _is_ephemeral(self, flashcard: Flashcard) -> bool:
        return flashcard.session is not None and flashcard.session.ephemeral

    def _apply_empty_state(self) -> None:
        empty = not self._flashcards
        self.query_one("#fv-empty", Static).display = empty
        self.query_one("#fv-list-scroll", VerticalScroll).display = not empty
        self.query_one("#fv-detail-panel", Vertical).display = not empty
        if empty:
            self.query_one("#fv-empty", Static).update("(No flashcards)")

    def _render_list(self) -> None:
        num_width = len(str(len(self._flashcards))) + 2

        # Build title strings from question text (truncated)
        max_title_len = 50
        titles: list[str] = []
        for fc in self._flashcards:
            q = fc.question_text.replace("\n", " ")
            if len(q) > max_title_len:
                q = q[: max_title_len - 1] + "\u2026"
            titles.append(q)

        max_title = max((len(t) for t in titles), default=0)

        text = Text()
        for i, fc in enumerate(self._flashcards):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            ephemeral = self._is_ephemeral(fc)
            marker = "\u25ba " if is_selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            title = titles[i]
            padding = max_title - len(title) + 2
            gap = " " * padding

            if is_selected and self.has_focus:
                style = f"bold {_FOCUS_GREEN}"
                marker_style = f"bold {_FOCUS_GREEN}"
            elif is_selected:
                style = "bold"
                marker_style = "bold"
            else:
                style = ""
                marker_style = ""

            text.append(marker, style=marker_style)
            text.append(num, style=style)
            text.append(title, style=style)

            if ephemeral:
                text.append(gap)
                text.append("(ephemeral)", style=_EPHEMERAL_DIM)

        self.query_one("#fv-list", Static).update(text)

    def _render_detail(self) -> None:
        if not self._flashcards:
            return
        idx = min(self.cursor, len(self._flashcards) - 1)
        fc = self._flashcards[idx]

        panel = self.query_one("#fv-detail-panel", Vertical)
        panel.border_title = f"Flashcard {idx + 1}"

        self.query_one("#fv-question", Static).update(fc.question_text)
        self.query_one("#fv-answer", Static).update(fc.answer_text)

        # Meta line
        parts: list[str] = []
        entry_count = len(fc.flashcard_entries) if fc.flashcard_entries else 0
        if entry_count == 1:
            parts.append("1 linked entry")
        else:
            parts.append(f"{entry_count} linked entries")
        if self._is_ephemeral(fc):
            parts.append("ephemeral")
        self.query_one("#fv-meta", Static).update("  ".join(parts))

        # Testing notes
        notes_label = self.query_one("#fv-notes-label", Static)
        notes_static = self.query_one("#fv-notes", Static)
        if fc.testing_notes:
            notes_label.display = True
            notes_static.display = True
            notes_static.update(fc.testing_notes)
        else:
            notes_label.display = False
            notes_static.display = False

    # ------------------------------------------------------------------
    # Focus changes
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        if self._flashcards:
            self.call_after_refresh(self._render_list)

    def on_blur(self) -> None:
        if self._flashcards:
            self.call_after_refresh(self._render_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self._flashcards and self.cursor > 0:
            self.cursor -= 1

    def action_cursor_down(self) -> None:
        if self._flashcards and self.cursor < len(self._flashcards) - 1:
            self.cursor += 1

    def action_dismiss(self) -> None:
        self.post_message(self.Dismissed())
