"""ToolMessageVM + view — a contiguous run of tool calls between agent text segments.

The VM holds an append-only list of ``(name, args)`` pairs. The view subscribes to ``dirty`` and
re-renders the box-drawing tree on each event. There is no streaming concept here: tool calls land
atomically, so a single render-on-dirty is enough.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static


from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.messages.tool import ToolMessageVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view


@register_feed_view(ToolMessageVM)
class ToolMessage(ViewBase[ToolMessageVM]):
    """Renders ``ToolMessageVM.tools`` as a Unicode box-drawing tree."""

    DEFAULT_CSS = """
    ToolMessage {
        color: $text-muted;
        padding: 0 0 0 4;
        height: auto;
        width: auto;
        min-width: 20;
    }
    ToolMessage #tool-title {
        width: auto;
        color: $text-muted;
    }
    ToolMessage #tool-content {
        width: auto;
    }
    """

    def __init__(self, vm: ToolMessageVM, **kwargs) -> None:
        super().__init__(vm, **kwargs)

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), id="tool-title")
        yield Static("", id="tool-content")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#tool-title", Static).update(self._title_text())
        self.query_one("#tool-content", Static).update(self._render_tree())

    def _title_text(self) -> str:
        c = "rgb(220, 160, 80)"
        return f"[{c}]tool calls[/{c}] ▼"

    def _max_arg_width(self) -> int:
        try:
            terminal_width = self.app.size.width
        except Exception:
            terminal_width = 80
        return max(int(terminal_width * 0.3), 20)

    def _render_tree(self) -> Text:
        output = Text()
        dim = "rgb(80,80,80)"
        max_width = self._max_arg_width()
        for i, (name, args) in enumerate(self._vm.tools):
            is_last_tool = i == len(self._vm.tools) - 1
            tool_prefix = "└── " if is_last_tool else "├── "
            if i > 0:
                output.append("\n")
            output.append(f"{tool_prefix}{name}")

            if args:
                arg_items = list(args.items())
                for j, (arg_name, arg_value) in enumerate(arg_items):
                    is_last_arg = j == len(arg_items) - 1
                    branch = "    " if is_last_tool else "│   "
                    arg_prefix = "└── " if is_last_arg else "├── "
                    text = repr(arg_value)
                    clipped = len(text) > max_width
                    if clipped:
                        text = text[:max_width - 3] + "…"
                    output.append(f"\n{branch}{arg_prefix}{arg_name}={text}", style=dim)
                    if clipped:
                        output.append("  (clipped)", style=dim)
        return output
