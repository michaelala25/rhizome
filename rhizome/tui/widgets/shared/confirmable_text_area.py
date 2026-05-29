"""``ConfirmableTextArea`` — a ``TextArea`` that emits an ``AcceptEditsRequested`` message on
``ctrl+j`` (the byte terminals usually send for ``ctrl+enter``).

Used wherever a details panel needs a "commit my edits" shortcut from inside an editable field.
The widget itself knows nothing about who owns it; the parent view catches
``ConfirmableTextArea.AcceptEditsRequested`` and decides what to do (typically: if the VM is
dirty, await ``vm.accept()``).
"""

from __future__ import annotations

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class ConfirmableTextArea(TextArea):
    """``TextArea`` with a ``ctrl+j`` → ``AcceptEditsRequested`` binding. We override the key at
    the widget level because ``TextArea`` consumes control keys before they bubble — a binding on
    a parent container would never see the keystroke."""

    BINDINGS = [
        Binding("ctrl+j", "accept_edits", show=False),
    ]

    class AcceptEditsRequested(Message):
        """User pressed ctrl+enter inside an editable details field."""

    def action_accept_edits(self) -> None:
        self.post_message(self.AcceptEditsRequested())
