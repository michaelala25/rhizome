"""Chat input — sub-VM + view used by the MVVM chat pane.

The input owns the visible buffer, the disabled/hint reconciliation, and
the per-session history ring. It holds a reference to the shared
``CommandPaletteViewModel`` (constructed by the pane) so that buffer
mutations can update palette filtering and so that Enter-on-visible-palette
can ask the palette directly whether the typed text is a complete command
— no widget-tree walks, no parent mediation.

The pane subscribes to the input's ``SUBMITTED`` callback group to learn
when to dispatch text (chat vs slash vs agent-busy gating all stays on
the pane). The pane also flips ``enabled`` / ``hint`` during interrupts.
"""

from __future__ import annotations

import time
from enum import Enum

from textual.events import Blur, Focus
from textual.widgets import TextArea

from ..view_model_base import ViewModelBase
from .command_palette import CommandPaletteViewModel


_BLURRED_HINT = "ctrl+l to return to the chat area"


class ChatInputViewModel(ViewModelBase):

    class Callbacks(Enum):
        SUBMITTED = "submitted"

    def __init__(
        self,
        palette: CommandPaletteViewModel,
        *,
        default_hint: str = "",
    ) -> None:
        super().__init__()

        self._submitted = self._make_group(ChatInputViewModel.Callbacks.SUBMITTED)

        self._palette = palette

        self.buffer: str = ""
        self.enabled: bool = True
        self.default_hint: str = default_hint
        self.hint: str = default_hint

        # Per-session history ring. ``index == -1`` means "not currently
        # navigating history" (the live buffer is the user's working draft).
        # On entry into history, the live buffer is preserved in ``_draft``
        # so down-arrow can restore it on exit.
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""

    # ------------------------------------------------------------------
    # Callback group accessors
    # ------------------------------------------------------------------

    @property
    def submitted(self):
        return self._submitted

    @property
    def palette(self) -> CommandPaletteViewModel:
        return self._palette

    @property
    def shell_mode(self) -> bool:
        """True when the buffer parses as a single-line ``!``-prefixed shell
        command. The view reads this to swap in the shell-mode border color."""
        return self.buffer.startswith("!") and "\n" not in self.buffer

    # ------------------------------------------------------------------
    # Buffer / state mutators
    # ------------------------------------------------------------------

    def set_buffer(self, text: str) -> None:
        if self.buffer == text:
            return
        self.buffer = text
        self._palette.update_for_input(text)
        self.emit(self.dirty)

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.emit(self.dirty)

    def set_hint(self, hint: str) -> None:
        if self.hint == hint:
            return
        self.hint = hint
        self.emit(self.dirty)

    def reset_hint(self) -> None:
        self.set_hint(self.default_hint)

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(self) -> None:
        """Fire SUBMITTED with the current buffer (stripped), clear the buffer,
        and record the entry in history. No-op on empty/whitespace-only
        buffers. Subscribers (the chat pane) decide chat-vs-slash and any
        agent-busy gating — the input itself doesn't know.
        """
        text = self.buffer.strip()
        if not text:
            return

        self._push_history(text)
        self.set_buffer("")
        self.emit(self.submitted, text)

    def _push_history(self, text: str) -> None:
        self._history.append(text)
        self._history_index = -1
        self._draft = ""

    # ------------------------------------------------------------------
    # History navigation
    # ------------------------------------------------------------------

    def can_history_prev(self) -> bool:
        if not self._history:
            return False
        if self._history_index == -1:
            return True
        return self._history_index > 0

    def history_prev(self) -> None:
        if not self.can_history_prev():
            return
        if self._history_index == -1:
            self._draft = self.buffer
            self._history_index = len(self._history) - 1
        else:
            self._history_index -= 1
        self.set_buffer(self._history[self._history_index])

    def can_history_next(self) -> bool:
        return self._history_index >= 0

    def history_next(self) -> None:
        if not self.can_history_next():
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.set_buffer(self._history[self._history_index])
        else:
            self._history_index = -1
            restored = self._draft
            self._draft = ""
            self.set_buffer(restored)

    # ------------------------------------------------------------------
    # Palette pass-throughs (so the view never touches the palette directly)
    # ------------------------------------------------------------------

    def move_palette_cursor(self, delta: int) -> None:
        self._palette.move_cursor(delta)

    def confirm_palette_selection(self) -> None:
        """Tab-completion: replace the buffer with ``/<selected> ``."""
        name = self._palette.selected_command
        if name is None:
            return
        self.set_buffer(f"/{name} ")

    def palette_has_exact_match(self) -> bool:
        return self._palette.has_exact_match(self.buffer)


class ChatInputView(TextArea):
    """View for ``ChatInputViewModel``.

    Subclasses ``TextArea`` rather than ``ViewBase`` so we keep the
    TextArea editing surface intact while still binding to a VM. Standard
    ``dirty`` subscription is wired manually in ``on_mount`` /
    ``on_unmount`` (matching the convention ``ViewBase`` codifies for the
    rest of the directory).

    All keystroke semantics that used to round-trip through the pane —
    Enter/Tab confirming a palette selection, up/down navigating history
    or the palette, double-Escape clearing — are handled here by calling
    the VM directly. The pane only learns about submissions via the
    ``submitted`` callback group.
    """

    def __init__(self, vm: ChatInputViewModel, *, id: str | None = None) -> None:
        super().__init__(
            show_line_numbers=False,
            tab_behavior="focus",
            id=id,
        )
        self._vm = vm
        self._last_escape: float = 0.0
        # Track previous enabled state to detect disabled→enabled transitions
        # (e.g. interrupt resolved) and refocus the input.
        self._prev_enabled: bool = vm.enabled

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → view
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self.placeholder != self._vm.hint:
            self.placeholder = self._vm.hint

        if self.disabled != (not self._vm.enabled):
            self.disabled = not self._vm.enabled

        # Refocus on a disabled→enabled transition (e.g. interrupt resolved).
        if self._vm.enabled and not self._prev_enabled:
            self.focus()
        self._prev_enabled = self._vm.enabled

        if self.text != self._vm.buffer:
            self.text = self._vm.buffer

        self.set_class(self._vm.shell_mode, "--shell-mode")

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def _move_cursor_to_end(self) -> None:
        """Place the cursor at the end of the buffer. Called after
        tab-completion so the user can keep typing args immediately.
        TextArea resets the cursor when ``text`` is reassigned in
        ``_refresh``, so we re-park it here after the VM round-trip."""
        self.move_cursor(self.document.end)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Echoes from our own _refresh write also land here; the VM no-ops
        # when the buffer hasn't changed, so we don't need to gate on it.
        event.stop()
        self._vm.set_buffer(event.text_area.text)

    def on_focus(self, event: Focus) -> None:
        # Restore the active hint on focus (the blur hook may have swapped
        # in the "ctrl+l to return" cue).
        self.placeholder = self._vm.hint

    def on_blur(self, event: Blur) -> None:
        if not self.disabled:
            self.placeholder = _BLURRED_HINT

    def _on_key(self, event) -> None:
        palette_visible = self._vm.palette.visible

        if event.key == "escape":
            now = time.monotonic()
            if now - self._last_escape < 0.5 and self.text:
                self._vm.set_buffer("")
                event.stop()
                event.prevent_default()
            self._last_escape = now
            return

        if event.key == "enter":
            if palette_visible and not self._vm.palette_has_exact_match():
                self._vm.confirm_palette_selection()
                self._move_cursor_to_end()
            else:
                self._vm.submit()
            event.stop()
            event.prevent_default()
            return

        if event.key == "tab" and palette_visible:
            self._vm.confirm_palette_selection()
            self._move_cursor_to_end()
            event.stop()
            event.prevent_default()
            return

        if event.key in ("up", "down") and palette_visible:
            self._vm.move_palette_cursor(-1 if event.key == "up" else 1)
            event.stop()
            event.prevent_default()
            return

        if event.key == "up":
            row, col = self.cursor_location
            if row == 0 and col == 0 and self._vm.can_history_prev():
                self._vm.history_prev()
                self.move_cursor((0, 0))
                event.stop()
                event.prevent_default()
                return
            super()._on_key(event)
            return

        if event.key == "down" and self._vm.can_history_next():
            self._vm.history_next()
            event.stop()
            event.prevent_default()
            return

        # Ctrl+Enter sends \n (0x0A) in most terminals, which Textual maps
        # to ctrl+j. Insert a literal newline.
        if event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return

        super()._on_key(event)  # pyright: ignore[reportUnusedCoroutine]
