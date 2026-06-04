"""``EditInstructionsArea`` — natural-language buffer at the bottom of the proposal widget.

Visible only when ``vm.edit_instructions_visible``. Two-tap-escape (within 500ms) clears the
buffer. ``ctrl+`` and ``alt+`` keys bubble so the parent's lifecycle / focus bindings win, except
``ctrl+a`` (select-all, handled locally via ``ProposalTextArea``'s binding) and ``ctrl+e`` (bubbles
via ``ProposalTextArea``'s ``SkipAction`` binding to the parent's ``toggle_edit_instructions``).
"""

from __future__ import annotations

import time

from textual import on
from textual.events import Key
from textual.widgets import TextArea

from rhizome.app.commit_proposal.commit_proposal import CommitProposalVM
from rhizome.tui.widgets.shared.text_area import ProposalTextArea


_DOUBLE_ESC_WINDOW = 0.5


class EditInstructionsArea(ProposalTextArea):
    """``ProposalTextArea`` bound to ``vm.edit_instructions``. Auto-hides via CSS when the VM flag is
    False.

    Owns its own escape-chord timer so the chord doesn't depend on parent-side bookkeeping.
    """

    DEFAULT_CSS = """
    EditInstructionsArea {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 8;
        padding: 0 1;
        margin: 0 2 0 1;
        display: none;
    }
    EditInstructionsArea.-visible {
        display: block;
    }
    EditInstructionsArea:focus {
        border: solid $accent;
    }
    """

    def __init__(self, vm: CommitProposalVM, **kwargs) -> None:
        super().__init__(show_line_numbers=False, soft_wrap=True, **kwargs)
        self._vm = vm
        self._last_escape_ts: float = 0.0
        self.border_title = "Edit instructions  ·  esc esc to clear"

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self._vm.edit_instructions_visible:
            self.add_class("-visible")
        else:
            self.remove_class("-visible")
        if self.text != self._vm.edit_instructions:
            self.text = self._vm.edit_instructions

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Belt-and-suspenders: in DONE the parent view flips ``can_focus`` False so the user can't
        # type here, but a programmatic load (e.g. a ``self.text =`` assignment from ``_refresh``)
        # would still fire this. Guard so we don't crash ``vm.set_edit_instructions``'s
        # ``_assert_editing``.
        if event.text_area is not self:
            return
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._vm.set_edit_instructions(event.text_area.text)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # ``up`` is owned by TextArea's BINDINGS (action_cursor_up), so it never reaches _on_key.
        # Reporting the binding inactive at row 0 lets the key bubble to the parent focus binding.
        if action == "cursor_up":
            row, _ = self.cursor_location
            return row > 0
        return True

    def _on_key(self, event: Key) -> None:
        # ctrl+a / ctrl+e: let the inherited BINDINGS run (select-all locally; SkipAction bubble to
        # the parent's toggle_edit_instructions).
        if event.key in ("ctrl+a", "ctrl+e"):
            return
        if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
            event.prevent_default()
            return
        if event.key == "escape":
            if self._vm.state != CommitProposalVM.State.EDITING:
                event.prevent_default()
                return
            now = time.monotonic()
            if now - self._last_escape_ts < _DOUBLE_ESC_WINDOW:
                self._vm.discard_edit_instructions()
                self._last_escape_ts = 0.0
                event.prevent_default()
                event.stop()
                return
            self._last_escape_ts = now
            event.prevent_default()
            return
        super()._on_key(event)
