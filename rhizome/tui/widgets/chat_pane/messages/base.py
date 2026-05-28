"""ChatMessage — a static, non-streaming chat message (user / system / error).

``ChatMessageData`` is an immutable dataclass — content is fixed at append time, so this view binds
to data, not a VM. Agent messages live in ``agent_message.py`` (separate VM + view, with streaming
drain).

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
from textual.widget import Widget
from textual.widgets import Markdown, Static

from rhizome.tui.types import Mode, Role


_ROLE_PREFIXES = {
    Role.USER: f"[bold rgb(100, 160, 230)]you:[/bold rgb(100, 160, 230)] ",
    Role.SYSTEM: f"[rgb(140, 140, 140)]system:[/rgb(140, 140, 140)] ",
    Role.ERROR: f"[bold rgb(220, 80, 80)]error:[/bold rgb(220, 80, 80)] ",
}


class ChatMessage(Widget):
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

    def __init__(
        self,
        *,
        role: Role,
        content: str,
        mode: Mode = Mode.IDLE,
        rich: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if role == Role.AGENT:
            raise ValueError(
                "ChatMessage does not render AGENT messages — use AgentMessage instead."
            )
        self._role = role
        self._prefix = _ROLE_PREFIXES.get(role, "")
        self._body = content
        self._rich = rich
        self.add_class(f"{role.value}-message")
        if mode == Mode.LEARN:
            self.add_class("learn-mode")
        elif mode == Mode.REVIEW:
            self.add_class("review-mode")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="msg-header"):
            yield Static(self._prefix, classes="msg-prefix")
        if self._rich:
            yield Static(Text.from_ansi(self._body), classes="msg-content")
        else:
            yield Markdown(self._body, classes="msg-content")
