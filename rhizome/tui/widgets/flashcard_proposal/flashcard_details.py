"""``FlashcardDetails`` — editable details pane on the right of the middle row.

Mirrors the structure of ``rhizome.tui.widgets.commit_proposal.entry_details.EntryDetails`` but
with four fields instead of two:

  - Question (`ProposalTextArea`, editable, dirty-tracked)
  - Answer (`ProposalTextArea`, editable, dirty-tracked)
  - Testing Notes (`ProposalTextArea`, editable, dirty-tracked)
  - Linked Entries (`Static`, read-only — outside the focus graph)

The Accept/Cancel ``ChoiceList`` appears only while ``FlashcardDetailsVM.is_dirty``, which is
derived from the three editable buffers.

Boundary navigation: plain ``up`` from the question and plain ``down`` from the bottom-most
visible field bubble to the parent's focus graph. Plain ``left`` from any field bubbles to the
parent so focus can return to the flashcard list. ``escape`` on any text area fires
``vm.cancel()`` (silent discard).
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import Static, TextArea

from rhizome.app.flashcard_proposal.flashcard_details import FlashcardDetailsVM
from rhizome.tui.widgets.shared.choices_list import ChoiceList
from rhizome.tui.widgets.shared.text_area import ProposalTextArea


class _FlashcardDetailChoices(ChoiceList[FlashcardDetailsVM]):
    """Accept/Cancel for the focused flashcard's question/answer/notes edit."""

    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Edit: "
    HINT = "ctrl+enter to accept · esc to reset"

    def _accept(self) -> None:
        self._vm.accept()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()


class _DetailsTextArea(ProposalTextArea):
    """``ProposalTextArea`` that bubbles ``alt+`` and ``ctrl+`` keys to the outer view so the
    parent's bindings (focus nav, lifecycle actions) win over the TextArea's default consumption.
    ``ctrl+a`` and ``ctrl+e`` are exempted so the inherited BINDINGS (select-all and the
    edit-instructions bubble) actually fire."""

    async def _on_key(self, event: Key) -> None:
        # ``TextArea._on_key`` is async in modern Textual; mirror the signature so the fall-
        # through path can await it without leaking a coroutine.
        if event.key in ("ctrl+a", "ctrl+e"):
            return
        if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
            event.prevent_default()
            return
        await super()._on_key(event)


class FlashcardDetails(Vertical):
    """View for ``FlashcardDetailsVM``. Subscribes to ``vm.dirty``; mirrors VM state into the
    three editable TextAreas, refreshes the read-only linked-entries display, and toggles the
    choices' visibility based on ``is_dirty``."""

    DEFAULT_CSS = """
    FlashcardDetails {
        width: 2fr;
        height: auto;
        padding: 0 1;
    }
    FlashcardDetails #fp-details-question {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 0 1;
    }
    FlashcardDetails #fp-details-question:focus {
        border: solid $accent;
    }
    FlashcardDetails #fp-details-answer {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 5;
        max-height: 12;
        padding: 0 1;
    }
    FlashcardDetails #fp-details-answer:focus {
        border: solid $accent;
    }
    FlashcardDetails #fp-details-testing-notes {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 2;
        max-height: 6;
        padding: 0 1;
    }
    FlashcardDetails #fp-details-testing-notes:focus {
        border: solid $accent;
    }
    FlashcardDetails #fp-details-linked-entries {
        background: transparent;
        border: solid #2a2a2a;
        border-title-align: right;
        border-title-color: rgb(100,100,100);
        color: rgb(140,140,140);
        height: auto;
        min-height: 1;
        padding: 0 1;
    }
    FlashcardDetails #fp-details-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    FlashcardDetails #fp-details-choices.-visible {
        display: block;
    }
    FlashcardDetails #fp-details-choices:focus {
        border-top: solid $accent;
    }
    """

    def __init__(self, vm: FlashcardDetailsVM, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vm = vm
        self._was_dirty = False

    def compose(self):
        question = _DetailsTextArea(
            id="fp-details-question", show_line_numbers=False, soft_wrap=True,
        )
        question.border_title = "Question"
        yield question

        answer = _DetailsTextArea(
            id="fp-details-answer", show_line_numbers=False, soft_wrap=True,
        )
        answer.border_title = "Answer"
        yield answer

        testing_notes = _DetailsTextArea(
            id="fp-details-testing-notes", show_line_numbers=False, soft_wrap=True,
        )
        testing_notes.border_title = "Testing Notes"
        yield testing_notes

        linked = Static("", id="fp-details-linked-entries")
        linked.border_title = "Linked Knowledge Entries"
        yield linked

        yield _FlashcardDetailChoices(self._vm, id="fp-details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        question_area = self.query_one("#fp-details-question", TextArea)
        answer_area = self.query_one("#fp-details-answer", TextArea)
        notes_area = self.query_one("#fp-details-testing-notes", TextArea)
        linked = self.query_one("#fp-details-linked-entries", Static)
        choices = self.query_one("#fp-details-choices", _FlashcardDetailChoices)

        if question_area.text != self._vm.question:
            question_area.text = self._vm.question
        if answer_area.text != self._vm.answer:
            answer_area.text = self._vm.answer
        if notes_area.text != self._vm.testing_notes:
            notes_area.text = self._vm.testing_notes

        # Linked entries — read-only mirror of the underlying flashcard's ids. Rendered as a flat
        # comma-separated list; ``(none)`` when empty so the bordered box still has something to
        # carry vertical height.
        ids = self._vm.original_entry_ids
        if ids:
            linked_text = Text(", ".join(f"#{i}" for i in ids), style="rgb(160,160,160)")
        else:
            linked_text = Text("(none)", style="dim")
        linked.update(linked_text)

        is_dirty = self._vm.is_dirty
        if is_dirty:
            if not self._was_dirty:
                choices.prepare_for_show()
            choices.add_class("-visible")
        else:
            # Focus-orphan rescue (mirrors commit-proposal entry-details).
            if (
                self._was_dirty
                and self.screen is not None
                and self.screen.focused is choices
            ):
                notes_area.focus()
            choices.remove_class("-visible")
        self._was_dirty = is_dirty

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        wid = event.text_area.id
        if wid == "fp-details-question":
            self._vm.set_question(event.text_area.text)
        elif wid == "fp-details-answer":
            self._vm.set_answer(event.text_area.text)
        elif wid == "fp-details-testing-notes":
            self._vm.set_testing_notes(event.text_area.text)

    @on(ProposalTextArea.AcceptEditsRequested)
    def _on_accept_edits_requested(
        self, event: ProposalTextArea.AcceptEditsRequested
    ) -> None:
        if self._vm.is_dirty:
            self._vm.accept()

    # ------------------------------------------------------------------
    # Boundary nav — bubble plain arrows on the text areas. The TextArea consumes arrows for
    # caret movement; we don't override that. Instead the parent watches focus and uses alt+arrow
    # exclusively for inter-region nav.
    # ------------------------------------------------------------------

    def on_key(self, event: Key) -> None:
        # Escape inside any editable field discards the buffer edit (reset to stored), matching
        # the commit-proposal ``esc auto-reset`` requirement.
        if event.key == "escape" and self._vm.is_dirty:
            self._vm.cancel()
            event.stop()
