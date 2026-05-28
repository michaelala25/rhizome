"""ToolCallList — renders a collapsible tree of tool calls with box-drawing characters."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from rhizome.tui.colors import Colors


class ToolCallList(Widget, can_focus=True):
    """Displays an ordered list of tool calls with args using Unicode box-drawing."""

    BINDINGS = [
        Binding("enter", "toggle_collapse", "Toggle collapse", show=False),
    ]

    DEFAULT_CSS = """
    ToolCallList {
        color: $text-muted;
        padding: 0 0 0 4;
        height: auto;
        width: auto;
        min-width: 20;
    }
    ToolCallList #tool-title {
        width: auto;
        color: $text-muted;
    }
    ToolCallList #tool-content {
        width: auto;
    }
    ToolCallList.--collapsed #tool-content {
        display: none;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tools: list[tuple[str, dict[str, Any]]] = []
        self._collapsed = False

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), id="tool-title")
        yield Static("", id="tool-content")

    def _title_text(self) -> str:
        c = Colors.TOOLCALL_TITLE
        hint = " [rgb(100,100,100)](ctrl+o)[/rgb(100,100,100)]" if self.has_class("--show-hint") else ""
        if self._collapsed:
            count = len(self._tools)
            s = "s" if count != 1 else ""
            return f"[{c}]{count} tool call{s}[/{c}] (click to expand...) ▶{hint}"
        return f"[{c}]tool calls[/{c}] ▼{hint}"

    def _update_title(self) -> None:
        self.query_one("#tool-title", Static).update(self._title_text())

    def _max_arg_width(self) -> int:
        """30% of terminal width, minimum 20 chars."""
        try:
            terminal_width = self.app.size.width
        except Exception:
            terminal_width = 80
        return max(int(terminal_width * 0.3), 20)

    def add_tool(self, name: str, args: dict[str, Any] | None = None) -> None:
        """Append a tool call and re-render."""
        self._tools.append((name, args or {}))
        self._render_list()

    def _render_list(self) -> None:
        output = Text()
        dim = "rgb(80,80,80)"
        max_width = self._max_arg_width()
        for i, (name, args) in enumerate(self._tools):
            is_last_tool = i == len(self._tools) - 1
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

        self.query_one("#tool-content", Static).update(output)

    def action_toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._update_title()
        self.toggle_class("--collapsed")

    def on_click(self) -> None:
        self.action_toggle_collapse()
