"""Autocomplete dropdown for slash commands."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class CommandPalette(Widget):
    """Filtered dropdown list of slash commands."""

    DEFAULT_CSS = """
    CommandPalette {
        display: none;
        height: auto;
        max-height: 10;
        width: 100%;
        background: $surface;
        border-top: solid rgb(60, 60, 60);
    }
    CommandPalette.visible {
        display: block;
    }
    CommandPalette .cmd-row {
        height: 1;
        padding: 0 1;
        color: $text-muted 70%;
    }
    CommandPalette .cmd-row.highlighted {
        background: rgb(86, 126, 160);
        color: #ffffff;
    }
    """

    filter_text: reactive[str] = reactive("", layout=True)
    selected_index: reactive[int] = reactive(0)

    class CommandSelected(Message):
        """Posted when the user picks a command from the palette."""

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    def _get_command_list(self) -> list[tuple[str, str]]:
        """Build command list from the parent ChatPane's registry."""
        from .chat_pane import ChatPane

        # Walk up to find the parent ChatPane
        node = self.parent
        while node is not None and not isinstance(node, ChatPane):
            node = node.parent
        if node is None:
            return []

        registry = node._command_registry
        items = []
        for name, cmd in sorted(registry.commands.items()):
            desc = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
            desc = desc.strip().split("\n")[0] if desc else ""
            items.append((name, desc))
        return items

    def _get_filtered(self) -> list[tuple[str, str]]:
        prefix = self.filter_text.lstrip("/")
        return [(n, d) for n, d in self._get_command_list() if n.startswith(prefix)]

    def watch_filter_text(self) -> None:
        self.selected_index = 0
        self._rebuild()

    def watch_selected_index(self) -> None:
        self._update_highlight()

    def _rebuild(self) -> None:
        """Rebuild the list of command rows."""
        filtered = self._get_filtered()
        # Remove old rows
        for child in list(self.children):
            child.remove()
        if not filtered:
            return
        max_name_len = max(len(n) for n, _ in filtered)
        for i, (name, desc) in enumerate(filtered):
            padded = name.ljust(max_name_len)
            row = Static(f"/{padded}  — {desc}", classes="cmd-row")
            row.set_class(i == self.selected_index, "highlighted")
            self.mount(row)

    def _update_highlight(self) -> None:
        rows = list(self.query(".cmd-row"))
        for i, row in enumerate(rows):
            row.set_class(i == self.selected_index, "highlighted")

    def move_selection(self, delta: int) -> None:
        """Move the selection up or down by *delta* items."""
        filtered = self._get_filtered()
        if not filtered:
            return
        self.selected_index = (self.selected_index + delta) % len(filtered)

    def confirm_selection(self) -> str | None:
        """Post a CommandSelected message for the current selection. Returns the name or None."""
        filtered = self._get_filtered()
        if not filtered:
            return None
        idx = min(self.selected_index, len(filtered) - 1)
        name = filtered[idx][0]
        self.post_message(self.CommandSelected(name))
        return name

    @property
    def has_items(self) -> bool:
        return len(self._get_filtered()) > 0
