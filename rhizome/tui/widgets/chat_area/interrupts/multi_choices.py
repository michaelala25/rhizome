"""MultipleChoices interrupt — MVVM port of the legacy ``multiple_choices.py`` widget.

Presents N questions, each with its own option list. The user moves between questions horizontally
(``ctrl+left`` / ``ctrl+right``) and chooses an answer per question vertically (``up`` / ``down`` /
``enter``). Once every question has an answer the VM enters a ``CONFIRMING`` phase showing a
``"Submit answers?"`` Yes/No prompt. Yes resolves the future with a ``dict[name -> answer]``; No
returns to ``ANSWERING`` with a sticky ``_has_confirmed_once`` flag so future "all answered"
transitions don't re-auto-confirm.

The VM owns the future (via ``InterruptModelBase``); the view is a passive projection that just
forwards key actions to VM mutators and re-renders on ``dirty``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.app.chat_area.interrupts.base import InterruptModelBase
from rhizome.app.chat_area.interrupts.multi_choices import MultiUserChoicesModel
from rhizome.tui.widgets.chat_area.feed_registry import register_feed_view

_DIM = "rgb(100,100,100)"
_ANSWERED = "rgb(100,200,100)"


@register_feed_view(MultiUserChoicesModel)
class MultiUserChoices(NavigableFeedItemViewBase[MultiUserChoicesModel]):
    """Three-region projection of ``MultiUserChoicesModel``: tab bar, prompt, options block, hint.

    After resolution the widget collapses to a single comma-separated summary line (no expand toggle —
    the legacy collapse button was intentionally dropped).
    """

    DEFAULT_CSS = """
    MultiUserChoices {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 0 2;
    }
    MultiUserChoices.--resolved {
        color: $text-muted;
    }
    MultiUserChoices #mc-tabs,
    MultiUserChoices #mc-prompt,
    MultiUserChoices #mc-options,
    MultiUserChoices #mc-hint,
    MultiUserChoices #mc-summary {
        height: auto;
        width: 1fr;
    }
    MultiUserChoices #mc-hint {
        color: $text-muted;
    }
    MultiUserChoices #mc-summary {
        display: none;
    }
    MultiUserChoices.--resolved #mc-tabs,
    MultiUserChoices.--resolved #mc-prompt,
    MultiUserChoices.--resolved #mc-options,
    MultiUserChoices.--resolved #mc-hint {
        display: none;
    }
    MultiUserChoices.--resolved #mc-summary {
        display: block;
    }
    """

    BINDINGS = [
        Keybind.CursorUp.             as_binding("move_cursor(-1)", "Up",                show=False),
        Keybind.CursorDown.           as_binding("move_cursor(1)",  "Down",              show=False),
        Keybind.MenuConfirm.          as_binding("confirm",         "Confirm",           show=True),
        Keybind.InnerFocusLeft.       as_binding("prev_question",   "Previous question", show=True),
        Keybind.InnerFocusRight.      as_binding("next_question",   "Next question",     show=True),
        # Legacy bound ctrl+enter to ctrl+j (Textual emits ctrl+j for ctrl+enter in many terminals).
        Keybind.InterruptSubmit.      as_binding("submit",          "Submit answers",    show=True, priority=True),
        Keybind.DialogCancel.         as_binding("cancel",          "Cancel",            show=True),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static(id="mc-tabs")
        yield Static(id="mc-prompt")
        yield Static(id="mc-options")
        yield Static(id="mc-hint")
        yield Static(id="mc-summary")

    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self._vm.resolved:
            self.add_class("--resolved")
            self._refresh_summary()
            return

        self._refresh_tabs()
        self._refresh_prompt()
        self._refresh_options()
        self._refresh_hint()

    def _refresh_tabs(self) -> None:
        Phase = MultiUserChoicesModel.Phase
        text = Text()
        for i, q in enumerate(self._vm.questions):
            if i > 0:
                text.append("  ")
            checked = "x" if i in self._vm.answers else " "
            label = f"[{checked}] {q['name']}"
            if self._vm.phase is Phase.ANSWERING and i == self._vm.active_question:
                text.append(label, style="bold white")
            elif i in self._vm.answers:
                text.append(label, style=_ANSWERED)
            else:
                text.append(label, style=_DIM)
        self.query_one("#mc-tabs", Static).update(text)

    def _refresh_prompt(self) -> None:
        Phase = MultiUserChoicesModel.Phase
        if self._vm.phase is Phase.CONFIRMING:
            self.query_one("#mc-prompt", Static).update("Submit answers?")
        else:
            q = self._vm.questions[self._vm.active_question]
            self.query_one("#mc-prompt", Static).update(q["prompt"])

    def _refresh_options(self) -> None:
        Phase = MultiUserChoicesModel.Phase

        if self._vm.phase is Phase.CONFIRMING:
            options = ["Yes", "No"]
            cursor = self._vm.confirm_cursor
            answered_option: str | None = None
        else:
            options = self._vm.questions[self._vm.active_question]["options"]
            cursor = self._vm.cursor
            answered_option = self._vm.answers.get(self._vm.active_question)

        text = Text()
        for i, option in enumerate(options):
            if i > 0:
                text.append("\n")
            label = f"  {i + 1}. {option}"
            if i == cursor:
                text.append(label, style="bold white")
            elif option == answered_option:
                text.append(label, style=_ANSWERED)
            else:
                text.append(label, style=_DIM)
        self.query_one("#mc-options", Static).update(text)

    def _refresh_hint(self) -> None:
        Phase = MultiUserChoicesModel.Phase
        if self._vm.phase is Phase.CONFIRMING:
            hint = "(enter to confirm, ctrl+c to cancel)"
        elif self._vm.all_answered:
            hint = "(ctrl+left/right to navigate, ctrl+enter to submit, ctrl+c to cancel)"
        else:
            hint = "(ctrl+left/right to navigate between questions, ctrl+c to cancel)"
        self.query_one("#mc-hint", Static).update(hint)

    def _refresh_summary(self) -> None:
        summary = self.query_one("#mc-summary", Static)
        if self._vm.cancelled:
            summary.update(Text("cancelled", style=_DIM))
            return
        answers = self._vm.answers
        parts = [
            f"{q['name']}: {answers.get(i, '—')}"
            for i, q in enumerate(self._vm.questions)
        ]
        summary.update(Text(", ".join(parts), style=_DIM))

    # ------------------------------------------------------------------
    # Actions — all pure VM forwards
    # ------------------------------------------------------------------

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()

    def action_prev_question(self) -> None:
        self._vm.prev_question()

    def action_next_question(self) -> None:
        self._vm.next_question()

    def action_submit(self) -> None:
        self._vm.submit()

    def action_cancel(self) -> None:
        self._vm.cancel()
