"""FlashcardProposal view — owns layout, focus routing, and key bindings.

Parallel to :class:`commit_proposal.view.CommitProposal`. Talks to
``FlashcardProposalViewModel`` through plain method calls; subscribes to
``vm.dirty`` for repaints. The VM has no knowledge of which Textual widget is
focused, which key fires which action, or which choices are visible.
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static, TextArea

from ..entry_list import ENTRY_ACCENT, ENTRY_DIM, ENTRY_HINT
from ..interrupt import InterruptWidgetBase
from ..view_base import ViewBase
from .view_model import FlashcardProposalViewModel


# ========================================================================================================================
# Style constants & static text
# ========================================================================================================================

_RED = ENTRY_ACCENT
_DIM = ENTRY_DIM
_HINT = ENTRY_HINT
_EXCLUDED = "rgb(60,60,60)"
_FOCUS_GREEN = "rgb(100,200,100)"

_DONE_GREEN = "rgb(120,210,110)"
_CANCEL_RED = "rgb(235,100,100)"

_HINTS_TEXT = (
    "  d: exclude/include  "
    "alt+←/→: cycle fields"
)

_EDIT_INSTRUCTIONS_TITLE = "alt+e to toggle  ·  esc esc to discard"

_CHOICE_APPROVE = "approve"
_CHOICE_REQUEST_EDITS = "request_edits"
_CHOICE_DISMISS_EDITS = "dismiss_edits"
_CHOICE_RESET = "reset"
_CHOICE_CANCEL = "cancel"

_CHOICE_LABELS: dict[str, str] = {
    _CHOICE_APPROVE: "Approve",
    _CHOICE_REQUEST_EDITS: "Edit",
    _CHOICE_DISMISS_EDITS: "Dismiss edits",
    _CHOICE_RESET: "Reset",
    _CHOICE_CANCEL: "Cancel",
}

_CHOICE_HINTS: dict[str, str] = {
    _CHOICE_APPROVE: "ctrl+a",
    _CHOICE_REQUEST_EDITS: "ctrl+e",
    _CHOICE_DISMISS_EDITS: "ctrl+e",
    _CHOICE_RESET: "ctrl+r",
    _CHOICE_CANCEL: "esc",
}

_CHOICE_DESCRIPTIONS: dict[str, str] = {
    _CHOICE_APPROVE: "accept the proposal (including any changes made above)",
    _CHOICE_REQUEST_EDITS: "describe the changes you'd like to make",
    _CHOICE_DISMISS_EDITS: "hide the edit-instructions area without discarding it",
    _CHOICE_RESET: "discard all changes and restore the original proposal",
    _CHOICE_CANCEL: "cancel the proposal",
}

_DOUBLE_ESC_WINDOW = 0.5


# ========================================================================================================================
# Child editors
# ========================================================================================================================


class EditInstructionsSubmit(Message):
    """Posted by ``_EditInstructions`` when the user presses Enter to submit."""


def _bubble_app_keys(event: events.Key) -> bool:
    if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
        event.prevent_default()
        return True
    return False


class _DetailArea(TextArea):
    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if not _bubble_app_keys(event):
            super()._on_key(event)


class _ChoicesList(Static, can_focus=True):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cursor: int | None = None


class _EditInstructions(TextArea):
    """Mirrors the edit-instructions widget in CommitProposal — see that file for the detailed rationale
    on ctrl/alt bubbling, the enter-to-submit hand-off, and the (0,0) up-out check_action trick.
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            self.post_message(EditInstructionsSubmit())
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return

        if _bubble_app_keys(event):
            return
        if event.key == "escape":
            event.prevent_default()
            return
        super()._on_key(event)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cursor_up":
            return self.cursor_location != (0, 0)
        return super().check_action(action, parameters)


# ========================================================================================================================
# FlashcardProposal
# ========================================================================================================================


class FlashcardProposal(
    ViewBase[FlashcardProposalViewModel],
    InterruptWidgetBase,
    can_focus=True,
):

    DISABLE_CHILDREN_ON_DEACTIVATE = False

    _CYCLE_TARGETS = (
        "outer",
        "fp-detail-question",
        "fp-detail-answer",
        "fp-detail-notes",
    )

    BINDINGS = [
        Binding("up,k", "cursor_up", show=False),
        Binding("down,j", "cursor_down", show=False),
        Binding("escape", "escape_chord", show=False),
        Binding("d", "toggle_exclude", show=False),
        Binding("alt+right", "cycle_field('forward')", show=False),
        Binding("alt+left", "cycle_field('back')", show=False),
        Binding("ctrl+e", "toggle_edit_instructions", show=False),
        Binding("alt+e", "swap_edit_focus", show=False),
        Binding("ctrl+a", "accept", show=False),
        Binding("ctrl+r", "reset", show=False),
        Binding("enter", "select_choice", show=False),
        Binding("enter", "toggle_collapsed", show=False),
    ]

    DEFAULT_CSS = """
    FlashcardProposal {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 1 0;
    }
    FlashcardProposal #fp-collapse {
        dock: right;
        width: auto;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: rgb(100,100,100);
        display: none;
    }
    FlashcardProposal #fp-collapse:hover {
        color: rgb(200,200,200);
    }
    FlashcardProposal #fp-collapse.-visible {
        display: block;
    }
    FlashcardProposal #fp-hints {
        color: rgb(80,80,80);
        margin-bottom: 1;
    }
    FlashcardProposal #fp-list-scroll {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
        scrollbar-size-vertical: 1;
    }
    FlashcardProposal #fp-list {
        height: auto;
    }
    FlashcardProposal #fp-detail {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    FlashcardProposal .fp-field-label {
        color: rgb(100,100,100);
        text-style: bold;
        height: 1;
        margin: 0;
    }
    FlashcardProposal #fp-detail-question,
    FlashcardProposal #fp-detail-answer,
    FlashcardProposal #fp-detail-notes {
        background: transparent;
        border: none;
        height: auto;
        min-height: 2;
        max-height: 8;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    FlashcardProposal #fp-detail-question:focus,
    FlashcardProposal #fp-detail-answer:focus,
    FlashcardProposal #fp-detail-notes:focus {
        border: solid $accent;
    }
    FlashcardProposal #fp-detail-entry-ids {
        color: rgb(100,100,100);
        height: 1;
    }
    FlashcardProposal #fp-choices {
        height: auto;
        color: rgb(150,150,150);
        margin-top: 1;
    }
    FlashcardProposal #fp-choices.-hidden {
        display: none;
    }
    FlashcardProposal #fp-edit-instructions {
        background: transparent;
        border: solid $surface-lighten-2;
        height: auto;
        min-height: 3;
        max-height: 8;
        margin: 1 0 0 0;
        padding: 0 1;
        display: none;
        border-title-align: right;
        border-title-color: rgb(80,80,80);
    }
    FlashcardProposal #fp-edit-instructions.-visible {
        display: block;
    }
    FlashcardProposal #fp-edit-instructions:focus {
        border: solid rgb(120,120,140);
    }
    FlashcardProposal #fp-resolution {
        height: 1;
        margin: 1 0 0 0;
        display: none;
    }
    FlashcardProposal #fp-resolution.-visible {
        display: block;
    }
    FlashcardProposal #fp-resolution.-centered {
        text-align: center;
    }
    FlashcardProposal #fp-resolution-edits {
        background: rgb(32,32,32);
        color: rgb(180,180,180);
        height: auto;
        padding: 1 2;
        margin: 1 0 0 0;
        display: none;
    }
    FlashcardProposal #fp-resolution-edits.-visible {
        display: block;
    }
    """

    # ========================================================================================================================
    # Construction
    # ========================================================================================================================

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> "FlashcardProposal":
        return cls(flashcards=value["flashcards"])

    def __init__(self, flashcards: list[dict[str, Any]], **kwargs) -> None:
        super().__init__(
            FlashcardProposalViewModel(flashcards=flashcards),
            **kwargs,
        )
        self._last_escape_at: float | None = None
        self._vm.subscribe(self._vm.dirty, self._maybe_resolve)

    def compose(self) -> ComposeResult:
        yield Button("▼", id="fp-collapse")
        yield Static("", id="fp-header")
        yield Static(Text(_HINTS_TEXT, style=_HINT), id="fp-hints")

        with VerticalScroll(id="fp-list-scroll"):
            yield Static("", id="fp-list")

        with Vertical(id="fp-detail"):
            yield Static("Question", classes="fp-field-label")
            yield _DetailArea(id="fp-detail-question")
            yield Static("Answer", classes="fp-field-label")
            yield _DetailArea(id="fp-detail-answer")
            yield Static("Testing Notes", classes="fp-field-label")
            yield _DetailArea(id="fp-detail-notes")
            yield Static("", id="fp-detail-entry-ids")

        yield _ChoicesList(id="fp-choices")
        yield _EditInstructions(id="fp-edit-instructions")

        yield Static("", id="fp-resolution")
        yield Static("", id="fp-resolution-edits")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#fp-edit-instructions", _EditInstructions).border_title = (
            _EDIT_INSTRUCTIONS_TITLE
        )
        self._refresh()

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.dirty, self._maybe_resolve)

    _EDITING_ONLY_ACTIONS = frozenset({
        "cycle_field",
        "toggle_exclude",
        "toggle_edit_instructions",
        "swap_edit_focus",
        "accept",
        "reset",
        "select_choice",
        "cancel_interrupt",
    })

    _DONE_ONLY_ACTIONS = frozenset({
        "toggle_collapsed",
    })

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self._EDITING_ONLY_ACTIONS:
            return self._vm.state == FlashcardProposalViewModel.State.EDITING
        if action in self._DONE_ONLY_ACTIONS:
            return self._vm.state == FlashcardProposalViewModel.State.DONE
        return super().check_action(action, parameters)


    # ========================================================================================================================
    # Event Handling
    # ========================================================================================================================

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._vm.state != FlashcardProposalViewModel.State.EDITING:
            return
        wid = event.text_area.id
        cur = self._vm.cursor
        if wid == "fp-detail-question" and cur is not None:
            self._vm.set_card_question(cur, event.text_area.text)
        elif wid == "fp-detail-answer" and cur is not None:
            self._vm.set_card_answer(cur, event.text_area.text)
        elif wid == "fp-detail-notes" and cur is not None:
            self._vm.set_card_testing_notes(cur, event.text_area.text)
        elif wid == "fp-edit-instructions":
            self._vm.set_edit_instructions(event.text_area.text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fp-collapse":
            event.stop()
            self._vm.toggle_collapsed()
            self.focus()

    def on_edit_instructions_submit(self, event: EditInstructionsSubmit) -> None:
        self._vm.accept()

    def _maybe_resolve(self) -> None:
        if self._future.done():
            return
        if self._vm.state != FlashcardProposalViewModel.State.DONE:
            return
        self.resolve(self._build_result(), deactivate_navigation=False)

    def _build_result(self) -> dict[str, Any]:
        # Mirrors the legacy "choice"/"flashcards"/"instructions" shape so the agent-side
        # consumer keeps working unchanged.
        if self._vm.cancelled:
            choice = "Cancel"
        elif self._vm.edit_instructions.strip():
            choice = "Edit"
        else:
            choice = "Approve"

        included = [
            {
                "question": f.question,
                "answer": f.answer,
                "testing_notes": f.testing_notes or None,
                "entry_ids": list(f.entry_ids),
            }
            for i, f in enumerate(self._vm.flashcards)
            if i not in self._vm.excluded
        ]

        result: dict[str, Any] = {"choice": choice, "flashcards": included}
        instructions = self._vm.edit_instructions.strip()
        if instructions:
            result["instructions"] = instructions
        return result


    # ========================================================================================================================
    # Rendering
    # ========================================================================================================================

    def _refresh(self) -> None:
        self._refresh_collapse_button()
        self._refresh_header()
        self._refresh_hints()
        self._refresh_list()
        self._refresh_detail()
        self._refresh_choices()
        self._refresh_edit_instructions()
        self._refresh_resolution()

    def _body_hidden(self) -> bool:
        return (
            self._vm.state == FlashcardProposalViewModel.State.DONE
            and self._vm.collapsed
        )

    def _refresh_collapse_button(self) -> None:
        btn = self.query_one("#fp-collapse", Button)
        in_done = self._vm.state == FlashcardProposalViewModel.State.DONE
        btn.set_class(in_done, "-visible")
        if in_done:
            btn.label = "▶" if self._vm.collapsed else "▼"

    def _refresh_hints(self) -> None:
        hints = self.query_one("#fp-hints", Static)
        hints.display = self._vm.state == FlashcardProposalViewModel.State.EDITING

    def _refresh_header(self) -> None:
        target = self.query_one("#fp-header", Static)
        if self._body_hidden():
            target.display = False
            return
        target.display = True

        text = Text()
        text.append("  Flashcard Proposal", style=f"bold {_RED}")
        n = len(self._vm.flashcards)
        suffix = "" if n == 1 else "s"
        text.append(f"  ({n} card{suffix})", style=_DIM)
        target.update(text)

    def _refresh_list(self) -> None:
        scroll = self.query_one("#fp-list-scroll", VerticalScroll)
        if self._body_hidden():
            scroll.display = False
            return
        scroll.display = True

        cards = self._vm.flashcards
        target = self.query_one("#fp-list", Static)

        if not cards:
            target.update(Text("(no flashcards)", style=_DIM))
            return

        num_width = len(str(len(cards))) + 2
        cursor = self._vm.cursor

        rows: list[Text] = []
        for i, fc in enumerate(cards):
            selected = cursor == i
            excluded = self._vm.is_excluded(i)
            marker_st, body_st = self._row_styles(selected=selected, excluded=excluded)

            marker = "► " if selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            question = fc.question.replace("\n", " ")

            row = Text()
            row.append(marker, style=marker_st)
            row.append(num, style=body_st)
            row.append(question, style=body_st)
            rows.append(row)

        target.update(Text("\n").join(rows))

        if cursor is not None:
            self._scroll_card_into_view(cursor)

    def _scroll_card_into_view(self, row: int) -> None:
        scroll = self.query_one("#fp-list-scroll", VerticalScroll)
        visible = scroll.size.height or 10
        y = scroll.scroll_offset.y
        if row < y:
            scroll.scroll_to(y=row, animate=False)
        elif row >= y + visible:
            scroll.scroll_to(y=row - visible + 1, animate=False)

    @staticmethod
    def _row_styles(*, selected: bool, excluded: bool) -> tuple[str, str]:
        marker = f"bold {_FOCUS_GREEN}" if selected else ""
        if excluded:
            return (marker, f"{_EXCLUDED} strike")
        if selected:
            return (marker, f"bold {_FOCUS_GREEN}")
        return ("", "")

    def _refresh_detail(self) -> None:
        detail = self.query_one("#fp-detail", Vertical)
        if self._body_hidden():
            detail.display = False
            return
        detail.display = True

        q_area = self.query_one("#fp-detail-question", TextArea)
        a_area = self.query_one("#fp-detail-answer", TextArea)
        n_area = self.query_one("#fp-detail-notes", TextArea)
        ids = self.query_one("#fp-detail-entry-ids", Static)

        cur = self._vm.cursor
        if cur is None:
            q_area.text = ""
            a_area.text = ""
            n_area.text = ""
            ids.update("")
            return

        fc = self._vm.flashcards[cur]
        if q_area.text != fc.question:
            q_area.text = fc.question
        if a_area.text != fc.answer:
            a_area.text = fc.answer
        if n_area.text != fc.testing_notes:
            n_area.text = fc.testing_notes

        if fc.entry_ids:
            ids_str = ", ".join(str(e) for e in fc.entry_ids)
            ids.update(Text(f"Linked entries: [{ids_str}]", style=_HINT))
        else:
            ids.update("")

    def _current_choices(self) -> list[str]:
        edit_label = (
            _CHOICE_DISMISS_EDITS
            if self._vm.edit_instructions_visible
            else _CHOICE_REQUEST_EDITS
        )
        return [_CHOICE_APPROVE, edit_label, _CHOICE_RESET, _CHOICE_CANCEL]

    def _refresh_choices(self) -> None:
        widget = self.query_one("#fp-choices", _ChoicesList)

        if self._vm.state != FlashcardProposalViewModel.State.EDITING:
            widget.add_class("-hidden")
            return
        widget.remove_class("-hidden")

        choices = self._current_choices()
        if widget.cursor is not None:
            widget.cursor = max(0, min(widget.cursor, len(choices) - 1))

        prefix_lengths = [
            2 + len(_CHOICE_LABELS[c]) + 1 + len(f"({_CHOICE_HINTS[c]})")
            for c in choices
        ]
        max_prefix = max(prefix_lengths)

        rows: list[Text] = []
        for i, choice in enumerate(choices):
            selected = i == widget.cursor
            label = _CHOICE_LABELS[choice]
            hint = _CHOICE_HINTS[choice]
            desc = _CHOICE_DESCRIPTIONS[choice]

            row = Text()
            if selected:
                row.append(f"► {label}", style=f"bold {_RED}")
            else:
                row.append(f"  {label}", style=_DIM)
            row.append(f" ({hint})", style=_HINT)
            padding = max_prefix - prefix_lengths[i] + 2
            row.append(" " * padding + desc, style=_HINT)
            rows.append(row)
        widget.update(Text("\n").join(rows))

    def _refresh_edit_instructions(self) -> None:
        area = self.query_one("#fp-edit-instructions", _EditInstructions)
        visible = (
            self._vm.state == FlashcardProposalViewModel.State.EDITING
            and self._vm.edit_instructions_visible
        )
        area.set_class(visible, "-visible")
        if not visible:
            return
        if area.text != self._vm.edit_instructions:
            area.text = self._vm.edit_instructions

    def _refresh_resolution(self) -> None:
        status = self.query_one("#fp-resolution", Static)
        edits_panel = self.query_one("#fp-resolution-edits", Static)

        if self._vm.state != FlashcardProposalViewModel.State.DONE:
            status.set_class(False, "-visible")
            edits_panel.set_class(False, "-visible")
            return

        cancelled = self._vm.cancelled
        has_edits = bool(self._vm.edit_instructions.strip())
        collapsed = self._vm.collapsed
        show_edits_panel = (not cancelled) and has_edits

        indent = "" if collapsed else "  "

        if cancelled:
            body = "Cancelled"
            color = _CANCEL_RED
        else:
            color = _DONE_GREEN
            if has_edits and not collapsed:
                body = "Accepted with edits:"
            elif has_edits:
                body = "Accepted with edits"
            else:
                body = "Accepted"

            if collapsed:
                kept = len(self._vm.flashcards) - len(self._vm.excluded)
                body += f" — {kept} card{'' if kept == 1 else 's'}"
                if self._vm.excluded:
                    body += f", {len(self._vm.excluded)} excluded"

        status.update(Text(f"{indent}{body}", style=color))
        status.set_class(True, "-visible")
        status.set_class(collapsed, "-centered")

        if show_edits_panel:
            edits_panel.update(self._vm.edit_instructions)
            edits_panel.set_class(True, "-visible")
        else:
            edits_panel.set_class(False, "-visible")


    # ========================================================================================================================
    # Bindings
    # ========================================================================================================================

    def action_cursor_up(self) -> None:
        if self._vm.state != FlashcardProposalViewModel.State.EDITING:
            self._vm.prev_card()
            return

        focused = self.app.focused
        choices = self.query_one("#fp-choices", _ChoicesList)
        edits = self.query_one("#fp-edit-instructions", _EditInstructions)

        if focused is self:
            self._vm.prev_card()
        elif focused is choices:
            if choices.cursor is not None and choices.cursor > 0:
                choices.cursor -= 1
            else:
                choices.cursor = None
                self.focus()
            self._refresh_choices()
        elif focused is edits:
            choices.cursor = len(self._current_choices()) - 1
            choices.focus()
            self._refresh_choices()

    def action_cursor_down(self) -> None:
        if self._vm.state != FlashcardProposalViewModel.State.EDITING:
            self._vm.next_card()
            return

        focused = self.app.focused
        choices = self.query_one("#fp-choices", _ChoicesList)
        edits = self.query_one("#fp-edit-instructions", _EditInstructions)

        if focused is self:
            if not self._vm.next_card():
                choices.focus()
                choices.cursor = 0
                self._refresh_choices()
        elif focused is choices:
            items = self._current_choices()
            if choices.cursor is None:
                choices.cursor = 0
            elif choices.cursor < len(items) - 1:
                choices.cursor += 1
            elif self._vm.edit_instructions_visible:
                choices.cursor = None
                edits.focus()
            self._refresh_choices()

    def action_escape_chord(self) -> None:
        edits = self.query_one("#fp-edit-instructions", _EditInstructions)
        if self.app.focused is not edits:
            return

        now = time.monotonic()
        if (
            self._last_escape_at is not None
            and now - self._last_escape_at < _DOUBLE_ESC_WINDOW
        ):
            self._vm.discard_edit_instructions()
            self._last_escape_at = None
        else:
            self._last_escape_at = now

    def action_toggle_exclude(self) -> None:
        self._vm.toggle_exclude_current_card()

    def action_accept(self) -> None:
        self._vm.accept()

    def action_reset(self) -> None:
        self._vm.reset()

    def action_toggle_edit_instructions(self) -> None:
        self._vm.toggle_edit_instructions_area()

        edits = self.query_one("#fp-edit-instructions", _EditInstructions)
        choices = self.query_one("#fp-choices", _ChoicesList)

        if self._vm.edit_instructions_visible:
            edits.focus()
            choices.cursor = None
        elif choices.cursor is not None:
            choices.focus()
        else:
            self.focus()

        self._refresh_choices()

    def action_swap_edit_focus(self) -> None:
        edits = self.query_one("#fp-edit-instructions", _EditInstructions)
        if self.app.focused is edits:
            self.focus()
            return
        if not self._vm.edit_instructions_visible:
            self._vm.toggle_edit_instructions_area()
        edits.focus()

    def action_cycle_field(self, direction: str) -> None:
        focused = self.app.focused
        focused_id = focused.id if focused is not None else None
        if focused is self or focused_id not in self._CYCLE_TARGETS:
            current = "outer"
        else:
            current = focused_id

        step = 1 if direction == "forward" else -1
        nxt = self._CYCLE_TARGETS[
            (self._CYCLE_TARGETS.index(current) + step) % len(self._CYCLE_TARGETS)
        ]
        target = self if nxt == "outer" else self.query_one(f"#{nxt}")
        target.focus()

    def action_select_choice(self) -> None:
        focused = self.app.focused
        choices = self.query_one("#fp-choices", _ChoicesList)

        if focused is not choices or choices.cursor is None:
            return

        action = [
            self._vm.accept,
            self.action_toggle_edit_instructions,
            self._vm.reset,
            self._vm.cancel,
        ][choices.cursor]

        action()

    def action_toggle_collapsed(self) -> None:
        self._vm.toggle_collapsed()

    def action_cancel_interrupt(self) -> None:
        self._vm.cancel()
