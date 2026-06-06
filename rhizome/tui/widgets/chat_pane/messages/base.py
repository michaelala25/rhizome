"""ChatMessage — view for ``ChatMessageModel``, a static (non-streaming) user / system / error message.

Binds to an immutable VM whose content is fixed at append time, so it holds no state and never
refreshes. Streaming agent output is a separate VM + view (``messages/agent.py``).

Two render modes:
  * ``rich=False`` (default) — markdown via Textual's ``Markdown`` widget. Used for normal user /
    system / error text.
  * ``rich=True`` — ANSI-rendered ``Static`` via Rich. Used for shell-command output and any other
    raw-terminal content the chat pane surfaces.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Markdown, Static

from rhizome.app.chat_pane.messages.static import ChatMessageModel
from rhizome.tui.types import Mode, Role
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view
from rhizome.tui.widgets.view_base import ViewBase


_ROLE_PREFIXES = {
    Role.USER: f"[bold rgb(100, 160, 230)]you:[/bold rgb(100, 160, 230)] ",
    Role.SYSTEM: f"[rgb(140, 140, 140)]system:[/rgb(140, 140, 140)] ",
    Role.ERROR: f"[bold rgb(220, 80, 80)]error:[/bold rgb(220, 80, 80)] ",
}


@register_feed_view(ChatMessageModel)
class ChatMessage(ViewBase[ChatMessageModel]):
    """Renders an immutable chat message with role prefix and styling."""

    DEFAULT_CSS = f"""
    ChatMessage {{
        padding: 1 2 0 2;
        height: auto;
    }}
    ChatMessage.user-message {{
        background: rgb(22, 22, 22);
        margin: 0 2;
    }}
    ChatMessage.system-message {{
        color: $text-muted;
    }}
    ChatMessage.error-message {{
        color: rgb(220, 80, 80);
    }}
    ChatMessage.error-message Markdown {{
        color: rgb(220, 80, 80);
    }}
    ChatMessage .msg-header {{
        height: auto;
        width: 1fr;
    }}
    ChatMessage .msg-prefix {{
        height: auto;
    }}
    ChatMessage .msg-content {{
        width: 1fr;
        color: rgb(204, 204, 204);
    }}
    """

    def __init__(self, vm: ChatMessageModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        if vm.role == Role.AGENT:
            raise ValueError(
                "ChatMessage does not render AGENT messages — use AgentMessage instead."
            )
        self._prefix = _ROLE_PREFIXES.get(vm.role, "")
        self.add_class(f"{vm.role.value}-message")
        if vm.mode == Mode.LEARN:
            self.add_class("learn-mode")
        elif vm.mode == Mode.REVIEW:
            self.add_class("review-mode")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="msg-header"):
            yield Static(self._prefix, classes="msg-prefix")
        if self._vm.rich:
            yield Static(Text.from_ansi(self._vm.content), classes="msg-content")
        else:
            yield Markdown(self._vm.content, classes="msg-content")
