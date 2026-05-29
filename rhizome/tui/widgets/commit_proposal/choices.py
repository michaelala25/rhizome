"""``CommitProposalChoices`` — vertical action menu at the bottom of the proposal widget.

Four always-present choices (Approve / Edit / Reset / Cancel) rendered one per line with the
keybinding column on the left and a short description trailing the label. The lifecycle bindings
on the parent ``CommitProposal`` (``ctrl+a/e/r/c``) and the in-menu cursor are intentionally
redundant — the user can either chord the binding from anywhere or arrow-navigate the menu and
press ``enter``.

Plain ``up`` / ``down`` move the menu cursor; at the first/last choice the binding is reported as
inactive via ``check_action`` so the keystroke bubbles to the parent's focus-graph bindings and
advances focus into the entry list or edit-instructions area.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text

from rhizome.app.commit_proposal.commit_proposal import CommitProposalVM
from rhizome.tui.widgets.browser.shared.choices_list import ChoiceList


class CommitProposalChoices(ChoiceList[CommitProposalVM]):

    DEFAULT_CSS = """
    CommitProposalChoices {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    ORIENTATION = "vertical"
    LEAD = None
    HINT = None

    CHOICES: ClassVar[dict[str, str]] = {
        "Approve": "_approve",
        "Edit": "_edit",
        "Reset": "_reset",
        "Cancel": "_cancel",
    }

    # Per-label keybinding + description shown alongside the choice. Kept here rather than in the
    # parent's BINDINGS list so the rendering owns the display strings outright.
    _ACTION_INFO: ClassVar[dict[str, tuple[str, str]]] = {
        "Approve": ("ctrl+a", "approve (including all user edits)"),
        "Edit":    ("ctrl+e", "toggle edit instructions"),
        "Reset":   ("ctrl+r", "reset all user edits"),
        "Cancel":  ("ctrl+c", "cancel/deny the commit proposal"),
    }

    # ------------------------------------------------------------------
    # Choice actions
    # ------------------------------------------------------------------

    def _approve(self) -> None:
        self._vm.accept_all()

    def _edit(self) -> None:
        self._vm.toggle_edit_instructions_area()
        if self._vm.edit_instructions_visible:
            try:
                self.screen.query_one("#cp-edit-instructions").focus()
            except Exception:
                pass

    def _reset(self) -> None:
        self._vm.reset()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()

    # ------------------------------------------------------------------
    # Boundary fall-through: in-list cursor while bounded, bubbles to the parent focus graph at
    # the top/bottom choice.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        n = len(self.choices())
        if action == "cursor_up":
            return self._cursor > 0
        if action == "cursor_down":
            return self._cursor < n - 1
        return True

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render_choice(self, label: str, selected: bool) -> Text:
        keybinding, description = self._ACTION_INFO[label]
        # Greys mirror the entry-list keybindings hint row for visual consistency: brighter
        # ``#a0a0a0`` for the key, dimmer ``#707070`` for the descriptive text. When blurred, the
        # cursor row collapses into the rest of the list (arrow loses highlight, label drops bold).
        focused = self.has_focus
        text = Text()
        if selected:
            text.append("► ", style="bold #ffd700" if focused else "#707070")
        else:
            text.append("  ")
        text.append(keybinding, style="#a0a0a0")
        text.append("  ")
        # Focused-cursor row promotes the label to white (matching the focused SharedTopicSetter)
        # and lifts the description one notch brighter than the resting grey.
        if selected and focused:
            label_style = "bold white"
            description_style = "#909090"
        else:
            label_style = "#a0a0a0"
            description_style = "#707070"
        text.append(f"{label:<7}", style=label_style)
        text.append(f"  - {description}", style=description_style)
        return text
