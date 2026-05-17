"""ChatPane view — steps 1+2 of the MVVM rewrite.

Layout: a ``VerticalScroll`` feed, a ``ChatInputView`` bound to
``vm.chat_input``, and a ``CommandPalette`` bound to the shared
``vm.command_palette``. The view subscribes to the VM's ``feed_append``
to mount one widget per appended feed entry and to ``feed_clear`` to
drop them all. All input-area keystroke handling (Enter, Tab, Up, Down,
Escape, Ctrl+Enter) lives inside ``ChatInputView`` itself, which talks
to the input VM directly — the pane is no longer in the path.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll

from ..message import ChatMessage, MarkdownChatMessage, RichChatMessage
from ..view_base import ViewBase
from .agent_message import AgentMessageView, AgentMessageViewModel
from .chat_input import ChatInputView
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
        yield ChatInputView(self._vm.chat_input, id="chat-input")
        yield CommandPalette(self._vm.command_palette, id="command-palette")

    def on_mount(self) -> None:
        self._vm.set_worker_scheduler(self.run_worker)
        self._vm.bootstrap_agent_session(
            self.app.options,  # type: ignore[attr-defined]
            debug=getattr(self.app, "debug_logging", False),
        )
        self.query_one("#chat-input", ChatInputView).focus()

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
    # Compatibility shims for the --new-chat-pane integration. These let
    # MainScreen / ChatTabPane treat the MVVM widget like the legacy
    # ChatPane for the limited surface they use; remove once the swap is
    # permanent.
    # ------------------------------------------------------------------

    def append_message(self, msg) -> None:
        self._vm.append_message(msg)
