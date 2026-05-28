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

from ..view_base import ViewBase
from rhizome.app.vm import ViewModelBase


class CommandPaletteViewModel(ViewModelBase):

    def __init__(self) -> None:
        super().__init__()
        self._all_commands: list[tuple[str, str]] = []
        self._filter_text: str = ""
        self._visible: bool = False
        self._cursor: int = 0

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def filter_text(self) -> str:
        return self._filter_text

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def filtered(self) -> list[tuple[str, str]]:
        prefix = self._name_prefix(self._filter_text)
        return [row for row in self._all_commands if row[0].startswith(prefix)]

    @property
    def selected_command(self) -> str | None:
        items = self.filtered
        if not items:
            return None
        idx = min(self._cursor, len(items) - 1)
        return items[idx][0]

    def has_exact_match(self, buffer_text: str) -> bool:
        """True if ``buffer_text`` parses to ``/<name>`` where ``<name>`` is a registered command. Used by the
        chat input to decide whether Enter on a visible palette should submit the command or confirm the
        selection (tab-completion).
        """
        name = self._name_prefix(buffer_text)
        if not name:
            return False
        return any(cmd_name == name for cmd_name, _ in self._all_commands)

    @staticmethod
    def _name_prefix(buffer_text: str) -> str:
        """Extract the command-name portion of a `/cmd ...` buffer."""
        stripped = buffer_text.lstrip("/")
        if not stripped:
            return ""
        return stripped.split(maxsplit=1)[0]

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        commands = sorted(commands, key=lambda r: r[0])
        if commands == self._all_commands:
            return
        self._all_commands = commands
        self._cursor = 0
        self.emit(self.dirty)

    def update_for_input(self, buffer_text: str) -> None:
        """Derive filter + visibility from a chat input buffer.

        Visible iff the buffer starts with ``/`` (no embedded newline) and there's at least one matching
        command. Cursor resets when the name-prefix changes.
        """
        is_command = buffer_text.startswith("/") and "\n" not in buffer_text
        next_filter = buffer_text if is_command else ""

        prev_prefix = self._name_prefix(self._filter_text)
        next_prefix = self._name_prefix(next_filter)

        # Snapshot then mutate, so the visibility check uses the new filter.
        self._filter_text = next_filter
        next_visible = is_command and bool(self.filtered)

        prefix_changed = prev_prefix != next_prefix
        visibility_changed = next_visible != self._visible

        if not prefix_changed and not visibility_changed:
            return

        if prefix_changed:
            self._cursor = 0
        self._visible = next_visible
        self.emit(self.dirty)

    def move_cursor(self, delta: int) -> None:
        items = self.filtered
        if not items:
            return
        self._cursor = (self._cursor + delta) % len(items)
        self.emit(self.dirty)


class CommandPalette(ViewBase[CommandPaletteViewModel]):

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

    def __init__(self, vm: CommandPaletteViewModel, **kwargs) -> None:
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
