"""``FlashcardPreview`` — read-only scrollable preview of the cursor flashcard's question +
answer + testing notes. ``can_focus=False`` so keyboard nav skips it (mouse-wheel scroll still
works)."""

from __future__ import annotations

from typing import Any

from textual.widgets import TextArea

from rhizome.app.browser.tabs.entries.linked_flashcards import LinkedFlashcardsPanelVM


class FlashcardPreview(TextArea):
    can_focus = False

    DEFAULT_CSS = """
    FlashcardPreview {
        background: transparent;
        border: solid #3a3a3a;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelVM,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            read_only=True, show_line_numbers=False, soft_wrap=True, **kwargs,
        )
        self._vm = view_model
        self.border_title = "[dim]question + answer + notes[/]"
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
            parts = ["Question:", fc.question_text, "", "Answer:", fc.answer_text]
            if fc.testing_notes:
                parts.extend(["", "Testing notes:", fc.testing_notes])
            target = "\n".join(parts)
        if self.text != target:
            self.text = target
