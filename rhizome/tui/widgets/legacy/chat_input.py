"""Multiline chat input: Enter submits, Ctrl+Enter inserts a newline."""

import time

from textual.message import Message
from textual.widgets import TextArea

from rhizome.tui.commands import parse_input


class ChatInput(TextArea):
    """A TextArea that submits on Enter and inserts newlines on Ctrl+Enter."""

    class Submitted(Message):
        """Posted when the user presses Enter to submit their message."""

        def __init__(self, input: "ChatInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    class PaletteNavigate(Message):
        """Posted to move the palette selection."""

        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class PaletteConfirm(Message):
        """Posted when the user presses Tab to confirm a palette selection."""

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(
            show_line_numbers=False,
            tab_behavior="focus",
            id=id,
        )
        self._placeholder = placeholder
        self.palette_active = False
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""
        self._last_escape: float = 0.0
        self.submit_empty: bool = False    # post Submitted even when input is empty
        self.suppress_history: bool = False  # disable up/down history navigation

    def push_history(self, text: str) -> None:
        """Record a submitted message in the history buffer."""
        self._history.append(text)
        self._history_index = -1
        self._draft = ""

    def on_mount(self) -> None:
        if self._placeholder:
            self.placeholder = self._placeholder

    def on_focus(self) -> None:
        self.placeholder = self._placeholder

    def on_blur(self) -> None:
        if not self.disabled:
            self.placeholder = "ctrl+l to return to the chat area"

    def _on_key(self, event) -> None:
        if event.key == "escape":
            now = time.monotonic()
            if now - self._last_escape < 0.5 and self.text:
                self.clear()
                event.stop()
                event.prevent_default()
            self._last_escape = now
            return

        if event.key == "enter":
            text = self.text.strip()
            if self.palette_active and not self._is_complete_command(text):
                self.post_message(self.PaletteConfirm())
                event.stop()
                event.prevent_default()
                return

            if text or self.submit_empty:
                self.post_message(self.Submitted(input=self, value=text))
            event.stop()
            event.prevent_default()

        elif event.key == "tab" and self.palette_active:
            self.post_message(self.PaletteConfirm())
            event.stop()
            event.prevent_default()

        elif event.key in ("up", "down") and self.palette_active:
            delta = -1 if event.key == "up" else 1
            self.post_message(self.PaletteNavigate(delta=delta))
            event.stop()
            event.prevent_default()

        elif event.key == "up" and not self.palette_active:
            row, col = self.cursor_location
            if row == 0 and col == 0 and self._history and not self.suppress_history:
                if self._history_index == -1:
                    self._draft = self.text
                    self._history_index = len(self._history) - 1
                elif self._history_index > 0:
                    self._history_index -= 1
                else:
                    event.stop()
                    event.prevent_default()
                    return
                self.clear()
                self.insert(self._history[self._history_index])
                self.move_cursor((0, 0))
                event.stop()
                event.prevent_default()
            else:
                super()._on_key(event)

        elif event.key == "down" and not self.palette_active and self._history_index >= 0 and not self.suppress_history:
            if self._history_index < len(self._history) - 1:
                self._history_index += 1
                self.clear()
                self.insert(self._history[self._history_index])
            else:
                self._history_index = -1
                self.clear()
                self.insert(self._draft)
                self._draft = ""
            event.stop()
            event.prevent_default()

        # Ctrl+Enter sends \n (0x0A) in most terminals, which Textual maps
        # to ctrl+j.  Insert a literal newline.
        elif event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
        else:
            super()._on_key(event)  # pyright: ignore[reportUnusedCoroutine]

    def _is_complete_command(self, text: str) -> bool:
        """Return True if *text* is a fully typed known command (e.g. '/explore')."""
        parsed = parse_input(text)
        if parsed is None:
            return False
        if parsed.name == "quit":
            return True

        # Walk up to find any ancestor exposing a command registry — works for
        # both the legacy ChatPane and the MVVM ChatPane passthrough.
        node = self.parent
        while node is not None and not hasattr(node, "_command_registry"):
            node = node.parent
        if node is None:
            return False
        return parsed.name in node._command_registry.commands
