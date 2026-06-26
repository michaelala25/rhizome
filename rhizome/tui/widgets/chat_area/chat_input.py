"""Chat input — sub-VM + view used by the MVVM chat area.

The view maps the VM's coarse ``State`` to the visible placeholder hint (display is a view concern; hint
*state* lives on the VM). It holds a reference to the shared ``CommandPaletteModel`` so that buffer
mutations can update palette filtering and so that Enter-on-visible-palette can ask the palette directly
whether the typed text is a complete command — no widget-tree walks, no parent mediation.

The chat area subscribes to the input's ``SUBMITTED`` callback group to learn when to dispatch text (chat vs
slash vs agent-busy gating all stays on the chat area). Owners flip the VM ``State`` during interrupts.
"""

from __future__ import annotations

import time
from enum import Enum

from textual import on
from textual.actions import SkipAction
from textual.events import Blur, Focus
from textual.widgets import TextArea

from rhizome.app.chat_area.chat_input import ChatInputModel
from rhizome.app.chat_area.command_palette import CommandPaletteModel


# Placeholder hints, inferred from the VM's coarse input state. The blurred cue below is a purely
# view-side overlay shown whenever an enabled input is unfocused; ``_refresh`` owns the choice between
# the two, so it stays correct even across transitions that fire no Blur/Focus event.
_STATE_HINTS = {
    ChatInputModel.State.CHAT: "Type a message or /command ...",
    ChatInputModel.State.COMMIT: "Type instructions for the commit (Enter to submit, may be empty)...",
    ChatInputModel.State.DISABLED: "",
    ChatInputModel.State.DISABLED_PENDING_INTERRUPT: "Resolve the prompt above to continue...",
}

_BLURRED_HINT = "ctrl+l to return to the chat area"


class ChatInput(TextArea):
    """View for ``ChatInputModel``.

    Subclasses ``TextArea`` rather than ``ViewBase`` so we keep the TextArea editing surface intact while
    still binding to a VM. Standard ``dirty`` subscription is wired manually in ``on_mount`` / ``on_unmount``
    (matching the convention ``ViewBase`` codifies for the rest of the directory).

    All keystroke semantics that used to round-trip through the chat area — Enter/Tab confirming a palette
    selection, up/down navigating history or the palette, double-Escape clearing — are handled here by
    calling the VM directly. The chat area only learns about submissions via the ``submitted`` callback group.
    """

    def __init__(self, vm: ChatInputModel, *, id: str | None = None) -> None:
        super().__init__(show_line_numbers=False, tab_behavior="focus", id=id)
        self._vm = vm
        self._last_escape: float = 0.0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._vm.subscribe(self._vm.Callbacks.RequestFocus, self.focus)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._vm.unsubscribe(self._vm.Callbacks.RequestFocus, self.focus)

    # ------------------------------------------------------------------
    # VM → view
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        # _refresh is the sole owner of the placeholder: an enabled-but-unfocused input shows the blurred
        # "ctrl+l to return" cue, everything else shows its state hint. Routing the focus cue through here
        # (rather than only on blur/focus) keeps it correct across focusless transitions too — e.g. swapping
        # to an unblocked branch re-enables the input while it sits unfocused, with no Blur to react to.
        hint = _BLURRED_HINT if (self._vm.enabled and not self.has_focus) else _STATE_HINTS[self._vm.state]
        if self.placeholder != hint:
            self.placeholder = hint

        if self.disabled != (not self._vm.enabled):
            self.disabled = not self._vm.enabled

        if self.text != self._vm.buffer:
            self.text = self._vm.buffer

        self.set_class(self._vm.shell_mode, "--shell-mode")

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def _move_cursor_to_end(self) -> None:
        """Place the cursor at the end of the buffer. Called after tab-completion so the user can keep typing
        args immediately. TextArea resets the cursor when ``text`` is reassigned in ``_refresh``, so we
        re-park it here after the VM round-trip."""
        self.move_cursor(self.document.end)

    @on(TextArea.Changed)
    def _on_buffer_changed(self, event: TextArea.Changed) -> None:
        # Echoes from our own _refresh write also land here; the VM no-ops when the buffer hasn't changed, so
        # we don't need to gate on it.
        event.stop()
        self._vm.set_buffer(event.text_area.text)

    def on_focus(self, event: Focus) -> None:
        # has_focus isn't yet updated inside the handler — defer to let _refresh read the settled state.
        self.call_after_refresh(self._refresh)

    def on_blur(self, event: Blur) -> None:
        self.call_after_refresh(self._refresh)

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

        # In COMMIT state, up/down skip history nav entirely and behave as plain TextArea cursor
        # movement so the user can navigate multi-line instructions.
        in_commit = self._vm.state == ChatInputModel.State.COMMIT

        if event.key == "up":
            row, col = self.cursor_location
            if not in_commit and row == 0 and col == 0 and self._vm.can_history_prev():
                self._vm.history_prev()
                self.move_cursor((0, 0))
                event.stop()
                event.prevent_default()
                return
            super()._on_key(event)
            return

        if event.key == "down" and not in_commit and self._vm.can_history_next():
            self._vm.history_next()
            event.stop()
            event.prevent_default()
            return

        # Ctrl+Enter sends \n (0x0A) in most terminals, which Textual maps to ctrl+j. Insert a literal
        # newline.
        if event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return

        super()._on_key(event)  # pyright: ignore[reportUnusedCoroutine]

    # ------------------------------------------------------------------
    # Ctrl+Left/Right: word-nav vs. panel hand-off
    # ------------------------------------------------------------------
    # When ``CtrlNavFromChatInput`` is on, plain Ctrl+Left/Right leave the input for the Workspace's outer
    # (panel) focus nav — ``SkipAction`` lets the key bubble past us to the ancestor binding for the same
    # key. Off, they stay word-wise cursor movement. Only the non-selecting variant is intercepted:
    # Ctrl+Shift+Left/Right (word selection) always edits text.

    def action_cursor_word_left(self, select: bool = False) -> None:
        if not select and self._vm.ctrl_panel_nav_enabled:
            raise SkipAction()
        super().action_cursor_word_left(select)

    def action_cursor_word_right(self, select: bool = False) -> None:
        if not select and self._vm.ctrl_panel_nav_enabled:
            raise SkipAction()
        super().action_cursor_word_right(select)
