"""FlashcardProposal — interrupt widget for reviewing agent-proposed flashcards."""

from __future__ import annotations

import copy
from enum import Enum, auto
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static, TextArea

from ..legacy.entry_list import ENTRY_ACCENT, ENTRY_DIM, ENTRY_HINT
from ..legacy.interrupt import InterruptWidgetBase

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHOICES = ["Approve", "Edit", "Reset", "Cancel"]
_CHOICE_HINTS = ["ctrl+a", "ctrl+e", "ctrl+r", "ctrl+c"]
_CHOICE_DESCRIPTIONS = [
    "accept the proposal (including any changes made above)",
    "describe the changes you'd like to make",
    "discard all changes and restore the original proposal",
    "cancel the proposal",
]

_RED = ENTRY_ACCENT
_DIM = ENTRY_DIM
_EXCLUDED_DIM = "rgb(60,60,60)"
_HINT = ENTRY_HINT
_FOCUS_GREEN = "rgb(100,200,100)"


class _State(Enum):
    BROWSE = auto()
    EDIT_DETAIL = auto()
    EDIT_INSTRUCTIONS = auto()


class _EditInstructions(TextArea):
    """Multiline input for edit instructions. Enter submits, Ctrl+J inserts a newline."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class NavigatedUp(Message):
        """Posted when the user presses up at the very start of the text area."""

    class FocusToggled(Message):
        """Posted when ctrl+e is pressed to toggle focus back to the list."""

    def _on_key(self, event) -> None:
        if event.key == "ctrl+e":
            self.post_message(self.FocusToggled())
            event.stop()
            event.prevent_default()
            return
        if event.key == "up":
            row, col = self.cursor_location
            if row == 0 and col == 0:
                self.post_message(self.NavigatedUp())
                event.stop()
                event.prevent_default()
                return
        if event.key == "enter":
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(value=text))
            event.stop()
            event.prevent_default()
        elif event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
        else:
            super()._on_key(event)


class FlashcardProposal(InterruptWidgetBase):
    """Displays a flashcard proposal for review with inline editing.

    Layout: flashcard list on the left, detail panel (question + answer)
    on the right, choices below.
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "select", show=False),
        Binding("escape", "escape", show=False),
        Binding("d", "toggle_exclude", show=False),
        Binding("ctrl+a", "approve", show=False),
        Binding("ctrl+e", "edit_instructions", show=False),
        Binding("ctrl+r", "reset_proposal", show=False),
        Binding("ctrl+c", "cancel_proposal", show=False),
    ]

    DEFAULT_CSS = """
    FlashcardProposal {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 1 0;
    }
    FlashcardProposal #fp-header {
        margin-bottom: 0;
    }
    FlashcardProposal #fp-hints {
        color: rgb(80,80,80);
        margin-bottom: 1;
    }
    FlashcardProposal #fp-split {
        height: auto;
    }
    FlashcardProposal #fp-list-pane {
        width: 35%;
        height: auto;
        margin-right: 1;
    }
    FlashcardProposal #fp-list {
        height: auto;
    }
    FlashcardProposal #fp-detail-pane {
        width: 65%;
        height: auto;
    }
    FlashcardProposal.stacked #fp-split {
        layout: vertical;
    }
    FlashcardProposal.stacked #fp-list-pane {
        width: 100%;
        margin-right: 0;
        margin-bottom: 1;
    }
    FlashcardProposal.stacked #fp-detail-pane {
        width: 100%;
    }
    FlashcardProposal #fp-detail {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    FlashcardProposal #fp-question-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin-bottom: 0;
    }
    FlashcardProposal #fp-question {
        background: transparent;
        border: none;
        height: auto;
        min-height: 2;
        max-height: 8;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    FlashcardProposal #fp-question:focus {
        border: solid $accent;
    }
    FlashcardProposal #fp-answer-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin-bottom: 0;
    }
    FlashcardProposal #fp-answer {
        background: transparent;
        border: none;
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 0;
        padding: 0 1;
    }
    FlashcardProposal #fp-answer:focus {
        border: solid $accent;
    }
    FlashcardProposal #fp-testing-notes-label {
        text-style: bold;
        color: rgb(100,100,100);
        margin-top: 1;
        margin-bottom: 0;
    }
    FlashcardProposal #fp-testing-notes {
        background: transparent;
        border: none;
        height: auto;
        min-height: 2;
        max-height: 6;
        margin: 0;
        padding: 0 1;
    }
    FlashcardProposal #fp-testing-notes:focus {
        border: solid $accent;
    }
    FlashcardProposal #fp-entry-ids {
        color: rgb(100,100,100);
        margin-top: 1;
    }
    FlashcardProposal #fp-choices {
        margin-top: 1;
    }
    FlashcardProposal #fp-edit-instructions {
        background: transparent;
        border: solid $surface-lighten-2;
        margin: 1 0 0 0;
        height: auto;
        min-height: 3;
        max-height: 8;
        padding: 0 1;
        border-title-align: right;
        border-title-color: rgb(80,80,80);
    }
    FlashcardProposal #fp-edit-instructions:focus {
        border: solid rgb(120,120,140);
    }
    FlashcardProposal #fp-user-instructions {
        background: $surface-lighten-1;
        border: none;
        padding: 0 1;
        margin: 1 0 0 0;
        height: auto;
    }
    """

    cursor: reactive[int] = reactive(0)

    def __init__(
        self,
        flashcards: list[dict[str, str]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._flashcards = [dict(fc) for fc in flashcards]
        self._original_flashcards = copy.deepcopy(self._flashcards)
        self._excluded: set[int] = set()
        self._state = _State.BROWSE

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> FlashcardProposal:
        return cls(flashcards=value["flashcards"])

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _card_count(self) -> int:
        return len(self._flashcards)

    @property
    def _total_items(self) -> int:
        return self._card_count + len(_CHOICES)

    @property
    def _cursor_card_index(self) -> int | None:
        """Return the 0-based card index if the cursor is on a card, else None."""
        if 0 <= self.cursor < self._card_count:
            return self.cursor
        return None

    @property
    def _viewed_card_index(self) -> int:
        """Card index to show in the detail panel (clamps to valid range)."""
        return min(self.cursor, self._card_count - 1)

    # ------------------------------------------------------------------
    # Compose & mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="fp-header")
        yield Static(id="fp-hints")
        with Horizontal(id="fp-split"):
            with Vertical(id="fp-list-pane"):
                yield Static(id="fp-list")
            with Vertical(id="fp-detail-pane"):
                with Vertical(id="fp-detail"):
                    yield Static("Question", id="fp-question-label")
                    yield TextArea(id="fp-question", show_line_numbers=False)
                    yield Static("Answer", id="fp-answer-label")
                    yield TextArea(id="fp-answer", show_line_numbers=False)
                    yield Static("Testing Notes", id="fp-testing-notes-label")
                    yield TextArea(id="fp-testing-notes", show_line_numbers=False)
                    yield Static(id="fp-entry-ids")
        yield Static(id="fp-choices")
        yield Static(id="fp-user-instructions")
        yield _EditInstructions(
            id="fp-edit-instructions",
            show_line_numbers=False,
        )

    def on_mount(self) -> None:
        super().on_mount()
        edit_inst = self.query_one("#fp-edit-instructions", _EditInstructions)
        edit_inst.display = False
        edit_inst.placeholder = "Describe what changes you'd like..."
        edit_inst.border_title = "ctrl+e to toggle focus"
        self.query_one("#fp-user-instructions", Static).display = False
        self.query_one("#fp-question", TextArea).cursor_blink = False
        self.query_one("#fp-answer", TextArea).cursor_blink = False
        self.query_one("#fp-testing-notes", TextArea).cursor_blink = False
        self._render_all()
        self.focus()

    _LIST_PROPORTION = 0.3
    _LIST_MIN_WIDTH = 40
    _UNSTACKED_LIST_PROPORTION = 0.8

    def on_resize(self, event) -> None:
        total_width = event.size.width

        # We should stack if we _cannot_ apportion a minimum number of characters for the list width
        should_stack = total_width * self._LIST_PROPORTION < self._LIST_MIN_WIDTH
        if should_stack and not self.has_class("stacked"):
            self.add_class("stacked")
            
        elif not should_stack and self.has_class("stacked"):
            self.remove_class("stacked")
            
        self._render_all()

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_cursor(self) -> None:
        if self._state in (_State.BROWSE, _State.EDIT_INSTRUCTIONS):
            self._render_list()
            self._render_detail()
            self._render_choices()
            self.call_after_refresh(self._scroll_choices_visible)

    def on_focus(self) -> None:
        self.call_after_refresh(self._render_list)

    def on_blur(self) -> None:
        self.call_after_refresh(self._render_list)

    # ------------------------------------------------------------------
    # Action gating
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool:
        browse_only = {"select", "toggle_exclude"}
        if action in browse_only:
            return self._state == _State.BROWSE
        if action in ("cursor_up", "cursor_down"):
            return self._state in (_State.BROWSE, _State.EDIT_INSTRUCTIONS)
        return True

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll_choices_visible(self) -> None:
        choices = self.query_one("#fp-choices", Static)
        choices.scroll_visible(animate=False)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_all(self) -> None:
        self._render_header()
        self._render_hints()
        self._render_list()
        self._render_detail()
        self._render_choices()

    def _render_header(self) -> None:
        n = self._card_count
        s = "s" if n != 1 else ""
        text = Text()
        text.append("  Flashcard Proposal", style=f"bold {_RED}")
        text.append(f"  ({n} card{s})", style=_DIM)
        self.query_one("#fp-header", Static).update(text)

    def _render_hints(self) -> None:
        self.query_one("#fp-hints", Static).update(Text(
            "  d: exclude/include  enter: edit  esc: back",
            style=_HINT,
        ))

    def _render_list(self) -> None:

        max_q_len = int(
            self.size.width * self._LIST_PROPORTION 
            if not self.has_class("stacked") 
            else self.size.width * self._UNSTACKED_LIST_PROPORTION
        )

        titles: list[str] = []
        for fc in self._flashcards:
            q = fc["question"].replace("\n", " ")
            if len(q) > max_q_len:
                q = q[: max_q_len - 1] + "\u2026"
            titles.append(q)

        num_width = len(str(self._card_count)) + 2

        focused = self.has_focus
        selected_style = f"bold {_FOCUS_GREEN}" if focused else "bold"

        text = Text()
        for i, fc in enumerate(self._flashcards):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            is_excluded = i in self._excluded

            marker = "\u25ba " if is_selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            title = titles[i]

            if is_excluded:
                style = f"{_EXCLUDED_DIM} strike"
            elif is_selected:
                style = selected_style
            else:
                style = ""

            text.append(marker, style=selected_style if is_selected else "")
            text.append(num, style=style)
            text.append(title, style=style)

        self.query_one("#fp-list", Static).update(text)

    def _render_detail(self) -> None:
        idx = self._viewed_card_index
        fc = self._flashcards[idx]

        panel = self.query_one("#fp-detail", Vertical)
        panel.border_title = f"Flashcard {idx + 1}"
        if idx in self._excluded:
            panel.border_subtitle = "(excluded)"
        else:
            panel.border_subtitle = None

        if self._state != _State.EDIT_DETAIL:
            q_area = self.query_one("#fp-question", TextArea)
            q_area.clear()
            q_area.insert(fc["question"])

            a_area = self.query_one("#fp-answer", TextArea)
            a_area.clear()
            a_area.insert(fc["answer"])

            tn_area = self.query_one("#fp-testing-notes", TextArea)
            tn_area.clear()
            notes = fc.get("testing_notes") or ""
            if notes:
                tn_area.insert(notes)

            entry_ids = fc.get("entry_ids", [])
            if entry_ids:
                ids_str = ", ".join(str(eid) for eid in entry_ids)
                self.query_one("#fp-entry-ids", Static).update(
                    Text(f"Linked entries: [{ids_str}]", style=_HINT)
                )
            else:
                self.query_one("#fp-entry-ids", Static).update("")

    def _render_choices(self) -> None:
        if self._state == _State.EDIT_INSTRUCTIONS:
            self._render_choices_edit_mode()
            return

        prefix_lengths = [
            2 + len(c) + 1 + len(f"({h})")
            for c, h in zip(_CHOICES, _CHOICE_HINTS)
        ]
        max_prefix = max(prefix_lengths)

        text = Text()
        for i, choice in enumerate(_CHOICES):
            if i > 0:
                text.append("\n")
            choice_idx = self._card_count + i
            is_selected = choice_idx == self.cursor
            hint = _CHOICE_HINTS[i]
            desc = _CHOICE_DESCRIPTIONS[i]
            if is_selected:
                text.append(f"\u25ba {choice}", style=f"bold {_RED}")
            else:
                text.append(f"  {choice}", style=_DIM)
            hint_str = f" ({hint})"
            text.append(hint_str, style=_HINT)
            padding = max_prefix - prefix_lengths[i] + 2
            text.append(" " * padding + desc, style=_HINT)
        self.query_one("#fp-choices", Static).update(text)

    def _render_choices_edit_mode(self) -> None:
        """Render the reduced choice set shown during EDIT_INSTRUCTIONS."""
        edit_choices = [("Reset", "ctrl+r"), ("Cancel", "ctrl+c")]
        text = Text()
        text.append("  enter to submit edit instructions\n", style=_HINT)
        for choice, hint in edit_choices:
            text.append(f"\n  {choice}", style=_DIM)
            text.append(f" ({hint})", style=_HINT)
        self.query_one("#fp-choices", Static).update(text)

    # ------------------------------------------------------------------
    # Browse actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1

    def action_cursor_down(self) -> None:
        if self._state == _State.EDIT_INSTRUCTIONS:
            if self.cursor >= self._card_count - 1:
                self.query_one("#fp-edit-instructions", _EditInstructions).focus()
                return
            self.cursor += 1
        elif self.cursor < self._total_items - 1:
            self.cursor += 1

    def action_select(self) -> None:
        if self.cursor < self._card_count:
            self._enter_detail_edit()
        else:
            choice = _CHOICES[self.cursor - self._card_count]
            self._handle_choice(choice)

    def action_toggle_exclude(self) -> None:
        card_idx = self._cursor_card_index
        if card_idx is not None:
            self._excluded.symmetric_difference_update({card_idx})
            self._render_list()
            self._render_detail()

    # ------------------------------------------------------------------
    # Escape — context-dependent
    # ------------------------------------------------------------------

    def action_escape(self) -> None:
        if self._state == _State.EDIT_DETAIL:
            self._save_detail_edits()
            self._state = _State.BROWSE
            self.query_one("#fp-question", TextArea).cursor_blink = False
            self.query_one("#fp-answer", TextArea).cursor_blink = False
            self.query_one("#fp-testing-notes", TextArea).cursor_blink = False
            self._render_list()
            self._render_detail()
            self.focus()
        elif self._state == _State.EDIT_INSTRUCTIONS:
            instructions_input = self.query_one("#fp-edit-instructions", _EditInstructions)
            instructions_input.display = False
            instructions_input.clear()
            self._state = _State.BROWSE
            self.focus()

    # ------------------------------------------------------------------
    # Detail editing
    # ------------------------------------------------------------------

    def _enter_detail_edit(self) -> None:
        self._state = _State.EDIT_DETAIL
        self.query_one("#fp-question", TextArea).cursor_blink = True
        self.query_one("#fp-answer", TextArea).cursor_blink = True
        self.query_one("#fp-testing-notes", TextArea).cursor_blink = True
        self.query_one("#fp-question", TextArea).focus()

    def _save_detail_edits(self) -> None:
        idx = self._viewed_card_index
        fc = self._flashcards[idx]
        fc["question"] = self.query_one("#fp-question", TextArea).text
        fc["answer"] = self.query_one("#fp-answer", TextArea).text
        notes = self.query_one("#fp-testing-notes", TextArea).text.strip()
        fc["testing_notes"] = notes if notes else None

    def on__edit_instructions_navigated_up(self, event: _EditInstructions.NavigatedUp) -> None:
        self.cursor = self._card_count - 1  # last card
        self.focus()

    def on__edit_instructions_focus_toggled(self, event: _EditInstructions.FocusToggled) -> None:
        self.focus()

    def on__edit_instructions_submitted(self, event: _EditInstructions.Submitted) -> None:
        self._resolve_edit(event.value)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "fp-edit-instructions":
            self.call_after_refresh(
                lambda: event.text_area.scroll_visible(animate=False)
            )

    # ------------------------------------------------------------------
    # Choice handling
    # ------------------------------------------------------------------

    def action_approve(self) -> None:
        if self._state != _State.EDIT_INSTRUCTIONS:
            self._handle_choice("Approve")

    def action_edit_instructions(self) -> None:
        if self._state == _State.EDIT_INSTRUCTIONS:
            # Toggle: refocus the text area from list browsing
            self.query_one("#fp-edit-instructions", _EditInstructions).focus()
        else:
            self._handle_choice("Edit")

    def action_reset_proposal(self) -> None:
        self._handle_choice("Reset")

    def action_cancel_proposal(self) -> None:
        self._handle_choice("Cancel")

    def _handle_choice(self, choice: str) -> None:
        if choice == "Approve":
            self._resolve(choice)
        elif choice == "Edit":
            self._state = _State.EDIT_INSTRUCTIONS
            self._render_choices()
            instructions_input = self.query_one("#fp-edit-instructions", _EditInstructions)
            instructions_input.display = True
            instructions_input.focus()
            self.call_after_refresh(
                lambda: instructions_input.scroll_visible(animate=False)
            )
        elif choice == "Reset":
            self._flashcards = copy.deepcopy(self._original_flashcards)
            self._excluded.clear()
            if self._state == _State.EDIT_INSTRUCTIONS:
                instructions_input = self.query_one("#fp-edit-instructions", _EditInstructions)
                instructions_input.display = False
                instructions_input.clear()
            self._state = _State.BROWSE
            self.cursor = 0
            self._render_all()
            self.focus()
        elif choice == "Cancel":
            self._resolve(choice)

    def _resolve(self, choice: str, instructions: str | None = None) -> None:
        if self._future.done():
            return
        included = [
            {**self._flashcards[i]}
            for i in range(self._card_count)
            if i not in self._excluded
        ]
        result: dict[str, Any] = {"choice": choice, "flashcards": included}
        if instructions:
            result["instructions"] = instructions
        self.resolve(result)
        self._render_resolved(choice, instructions)

    def _resolve_edit(self, instructions: str) -> None:
        self._resolve("Edit", instructions=instructions)

    def _render_resolved(self, choice: str, instructions: str | None = None) -> None:
        """Dim the widget after resolution."""
        self.query_one("#fp-question", TextArea).cursor_blink = False
        self.query_one("#fp-answer", TextArea).cursor_blink = False
        self.query_one("#fp-testing-notes", TextArea).cursor_blink = False
        resolved = Text()
        if choice == "Approve":
            resolved.append("  Approved", style=_DIM)
        elif choice == "Cancel":
            resolved.append("  Cancelled", style=_DIM)
        elif choice == "Edit":
            resolved.append("  Editing...", style=_DIM)
        else:
            resolved.append(f"  {choice}", style=_DIM)
        self.query_one("#fp-choices", Static).update(resolved)
        self.query_one("#fp-hints", Static).update("")
        self.query_one("#fp-edit-instructions", _EditInstructions).display = False
        if instructions:
            user_msg = self.query_one("#fp-user-instructions", Static)
            user_msg.update(Text(f"user: {instructions}", style=_DIM))
            user_msg.display = True

