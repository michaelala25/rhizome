"""ChatPane view — steps 1+2 of the MVVM rewrite.

Layout: a ``VerticalScroll`` feed, a ``ChatInput``, and a
``CommandPalette`` panel bound to the VM's palette sub-VM. The view
subscribes to the VM's ``feed_append`` to mount one ``ChatMessage`` per
appended feed entry, to ``feed_clear`` to drop them all, and forwards
``ChatInput``'s palette-nav messages into the VM. ``_refresh`` reconciles
the chat input (placeholder, disabled, text, palette_active).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import TextArea

from ..chat_input import ChatInput
from ..message import ChatMessage, MarkdownChatMessage, RichChatMessage
from ..view_base import ViewBase
from .agent_message import AgentMessageView, AgentMessageViewModel
from .command_palette import CommandPalette
from .interrupt import InterruptViewModelBase, TestInterruptView, TestInterruptViewModel
from .view_model import ChatPaneViewModel
from rhizome.tui.types import ChatMessageData


FeedEntryWidget = ChatMessage | AgentMessageView | TestInterruptView


class ChatPaneMVVM(ViewBase[ChatPaneViewModel]):

    DEFAULT_CSS = """
    ChatPaneMVVM {
        layout: vertical;
        height: 1fr;
    }
    ChatPaneMVVM #message-area {
        height: 1fr;
        background: $surface-darken-1;
        padding: 1;
        scrollbar-color: rgb(60, 60, 60);
        scrollbar-color-hover: rgb(80, 80, 80);
        scrollbar-color-active: rgb(100, 100, 100);
    }
    ChatPaneMVVM #chat-input {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
    }
    ChatPaneMVVM CommandPalette {
        background: rgb(12, 12, 12);
    }
    """

    def __init__(self, *, session_factory=None, **kwargs) -> None:
        super().__init__(ChatPaneViewModel(session_factory=session_factory), **kwargs)

        # Mounted widgets in feed order. Indices line up with vm.feed so ping/clear
        # callbacks can reach them directly. The element type depends on the
        # corresponding feed entry: ChatMessage for ChatMessageData,
        # AgentMessageView for AgentMessageViewModel.
        self._mounted: list[FeedEntryWidget] = []

        self._vm.subscribe(self._vm.feed_append, self._on_feed_append)
        self._vm.subscribe(self._vm.feed_clear, self._on_feed_clear)

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.feed_append, self._on_feed_append)
        self._vm.unsubscribe(self._vm.feed_clear, self._on_feed_clear)

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="message-area")
        yield ChatInput(id="chat-input")
        yield CommandPalette(self._vm.command_palette, id="command-palette")

    # Passthrough so ``ChatInput._is_complete_command`` can find the registry by
    # walking up the widget tree (the legacy widget exposes it as an attribute).
    @property
    def _command_registry(self):
        return self._vm.command_registry

    def on_mount(self) -> None:
        self._vm.bootstrap_agent_session(
            self.app.options,  # type: ignore[attr-defined]
            debug=getattr(self.app, "debug_logging", False),
        )
        self._refresh()
        self.query_one("#chat-input", ChatInput).focus()

    # ------------------------------------------------------------------
    # VM → view callbacks
    # ------------------------------------------------------------------

    def _on_feed_append(self, idx: int) -> None:
        entry = self._vm.feed[idx]
        area = self.query_one("#message-area", VerticalScroll)

        if isinstance(entry, ChatMessageData):
            cls = RichChatMessage if entry.rich else MarkdownChatMessage
            widget: FeedEntryWidget = cls(
                role=entry.role, content=entry.content, mode=entry.mode
            )
        elif isinstance(entry, AgentMessageViewModel):
            widget = AgentMessageView(entry)
        elif isinstance(entry, TestInterruptViewModel):
            widget = TestInterruptView(entry)
        elif isinstance(entry, InterruptViewModelBase):
            raise TypeError(
                f"No view registered for interrupt type: {type(entry).__name__}"
            )
        else:
            raise TypeError(f"Unhandled feed entry type: {type(entry).__name__}")
        
        area.mount(widget)
        self._mounted.append(widget)
        area.scroll_end(animate=False)

    def _on_feed_clear(self) -> None:
        for widget in self._mounted:
            widget.remove()
            
        self._mounted.clear()

    # ------------------------------------------------------------------
    # Input area sync
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        chat_input = self.query_one("#chat-input", ChatInput)

        # ChatInput's own _placeholder is read on focus/blur to repaint, so we set
        # both the cached value and the live attribute to keep them in sync.
        if chat_input._placeholder != self._vm.input_hint:
            chat_input._placeholder = self._vm.input_hint
            chat_input.placeholder = self._vm.input_hint

        if chat_input.disabled != (not self._vm.input_enabled):
            chat_input.disabled = not self._vm.input_enabled
            
        if chat_input.text != self._vm.input_buffer:
            chat_input.text = self._vm.input_buffer

        # Drives ChatInput's Enter/Tab/Up/Down branching when a slash command
        # is being typed. The palette's own widget reflects vm.command_palette.
        palette_visible = self._vm.command_palette.visible
        if chat_input.palette_active != palette_visible:
            chat_input.palette_active = palette_visible

    # ------------------------------------------------------------------
    # ChatInput → VM
    # ------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "chat-input":
            return
        # Echoes from our own _refresh() write also land here; the VM no-ops when
        # the buffer hasn't changed, so we don't need to gate on it.
        self._vm.set_user_input_buffer(event.text_area.text)

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        event.stop()
        self._vm.submit_user_input()

    def on_chat_input_palette_navigate(self, event: ChatInput.PaletteNavigate) -> None:
        event.stop()
        self._vm.move_palette_cursor(event.delta)

    def on_chat_input_palette_confirm(self, event: ChatInput.PaletteConfirm) -> None:
        event.stop()
        self._vm.confirm_palette_selection()

    # ------------------------------------------------------------------
    # Compatibility shims for the --new-chat-pane integration. These let
    # MainScreen / ChatTabPane treat the MVVM widget like the legacy
    # ChatPane for the limited surface they use; remove once the swap is
    # permanent.
    # ------------------------------------------------------------------

    def append_message(self, msg) -> None:
        self._vm.append_message(msg)
