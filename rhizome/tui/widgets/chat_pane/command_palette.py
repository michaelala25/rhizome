"""Slash-command palette — sub-VM + view used by the MVVM chat pane.

The palette knows nothing about the chat pane: the parent VM hands it a list of ``(name, description)``
rows, pushes the current input buffer into ``update_for_input``, and reads ``selected_command`` on
tab-confirm. The palette owns its own filtering, visibility, cursor, and dirty channel; the view
subscribes to it directly.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.command_palette import CommandPaletteModel


class CommandPalette(ViewBase[CommandPaletteModel]):

    DEFAULT_CSS = """
    CommandPalette {
        display: none;
        height: auto;
        max-height: 10;
        width: 100%;
        background: $surface;
        border-top: solid rgb(60, 60, 60);
    }
    CommandPalette.-visible {
        display: block;
    }
    CommandPalette > Static {
        height: auto;
        padding: 0 1;
        color: $text-muted 70%;
    }
    """

    def __init__(self, vm: CommandPaletteModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)

    def compose(self) -> ComposeResult:
        yield Static(id="command-rows")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        visible = self._vm.visible
        self.set_class(visible, "-visible")
        if not visible:
            return

        rows = self._vm.filtered
        cursor = min(self._vm.cursor, len(rows) - 1) if rows else 0
        max_name = max(len(name) for name, _ in rows) if rows else 0

        text = Text()
        for i, (name, desc) in enumerate(rows):
            padded = name.ljust(max_name)
            line_style = "reverse" if i == cursor else "dim"
            line = Text(f"/{padded}  — {desc}\n", style=line_style)
            text.append(line)
        # Strip the trailing newline so the widget doesn't leave a blank row.
        if text.plain.endswith("\n"):
            text.right_crop(1)

        self.query_one("#command-rows", Static).update(text)
