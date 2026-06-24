"""ChatMessageModel — a static, non-streaming chat message (user / system / error) in the feed.

The content is fixed at append time, so this VM holds no mutable state and never emits ``dirty`` —
its view is a dumb mirror. Streaming agent output is a separate concern (see ``messages/agent.py``).
"""

from __future__ import annotations

from rhizome.app.model import ViewModelBase
from rhizome.tui.types import Mode, Role


class ChatMessageModel(ViewModelBase):

    def __init__(self, role: Role, content: str, mode: Mode = Mode.IDLE, rich: bool = False) -> None:
        super().__init__()
        self.role = role
        self.content = content
        self.mode = mode
        self.rich = rich
