"""ToolMessageModel + view — a contiguous run of tool calls between agent text segments.

The VM holds an append-only list of ``(name, args)`` pairs. The view subscribes to ``dirty`` and
re-renders the box-drawing tree on each event. There is no streaming concept here: tool calls land
atomically, so a single render-on-dirty is enough.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static


from rhizome.app.model import ViewModelBase


class ToolMessageModel(ViewModelBase):
    """An ordered list of tool calls. Append-only via ``add_tool_call``."""

    def __init__(self) -> None:
        super().__init__()
        self.tools: list[tuple[str, dict[str, Any]]] = []

    def add_tool_call(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.tools.append((name, args or {}))
        self.emit(self.dirty)
