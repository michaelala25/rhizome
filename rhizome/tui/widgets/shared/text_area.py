"""Shared ``TextArea`` subclasses used across the TUI.

``ConfirmableTextArea`` is the generic editable-field variant: a ``TextArea`` that emits an
``AcceptEditsRequested`` message on ``ctrl+j`` (the byte terminals usually send for ``ctrl+enter``).
The widget itself knows nothing about who owns it; the parent view catches the message and decides
what to do (typically: if the VM is dirty, await ``vm.accept()``). It also rebinds the inherited
emacs-style ``ctrl+a`` away from ``cursor_line_start`` over to ``select_all`` — ``home`` still maps
to ``cursor_line_start``.

``ProposalTextArea`` is the variant used inside the chat-pane proposal widgets (commit / flashcard).
It additionally bubbles ``ctrl+e`` so the parent view's "toggle edit instructions" binding wins —
``end`` is left bound to ``cursor_line_end``.
"""

from __future__ import annotations

from textual.actions import SkipAction
from textual.message import Message
from textual.widgets import TextArea

from rhizome.tui.keybindings import Keybind


class ConfirmableTextArea(TextArea):
    """``TextArea`` with ``ctrl+j`` → ``AcceptEditsRequested`` and ``ctrl+a`` → ``select_all``.
    See the module docstring for the rationale."""

    BINDINGS = [
        Keybind.EditAccept.   as_binding("accept_edits", show=False),
        Keybind.EditSelectAll.as_binding("select_all",   show=False),
    ]

    class AcceptEditsRequested(Message):
        """User pressed ctrl+enter inside an editable details field."""

    def action_accept_edits(self) -> None:
        self.post_message(self.AcceptEditsRequested())


class ProposalTextArea(ConfirmableTextArea):
    """``ConfirmableTextArea`` for the proposal widgets — additionally bubbles ``ctrl+e`` so the
    parent's ``toggle_edit_instructions`` binding fires even with this TextArea focused."""

    BINDINGS = [
        Keybind.ProposalToggleEditInstructions.as_binding("bubble_ctrl_e", show=False),
    ]

    def action_bubble_ctrl_e(self) -> None:
        raise SkipAction()
