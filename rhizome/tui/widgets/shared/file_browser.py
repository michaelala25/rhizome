"""FileBrowser — ncdu-style single-directory file browser."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


_DIM = "rgb(100,100,100)"
_DIR_COLOR = "rgb(180,180,180)"
_FILE_COLOR = "rgb(140,140,140)"
_CURSOR_FOCUSED = "rgb(255,80,80)"
_CURSOR_UNFOCUSED = "rgb(180,60,60)"
_HEADER_COLOR = "rgb(80,80,80)"


class FileBrowser(Widget, can_focus=True):
    """Single-directory file browser with ncdu-style navigation.

    Left navigates to parent, right enters a directory.
    Directories are listed before files, both sorted alphabetically.
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("left", "go_parent", show=False),
        Binding("right", "enter_dir", show=False),
        Binding("enter", "select", show=False),
        Binding("home", "go_home", show=False),
        Binding("escape", "dismiss", show=False),
    ]

    DEFAULT_CSS = """
    FileBrowser {
        height: auto;
        layout: vertical;
    }
    FileBrowser #fb-header {
        color: rgb(80,80,80);
        margin: 0 0 0 1;
    }
    FileBrowser #fb-list-scroll {
        height: auto;
        max-height: 20;
    }
    FileBrowser #fb-list {
        height: auto;
        padding: 0 1;
    }
    FileBrowser #fb-hint {
        color: rgb(80,80,80);
        margin: 0 0 0 1;
    }
    """

    class FileSelected(Message):
        """Posted when the user presses Enter on a file."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    class Dismissed(Message):
        """Posted when the user presses Escape."""

    cursor: reactive[int] = reactive(0)

    def __init__(self, start_path: Path | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._home = (start_path or Path.cwd()).resolve()
        self._cwd = self._home
        self._entries: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="fb-header")
        with VerticalScroll(id="fb-list-scroll"):
            yield Static(id="fb-list")
        yield Static("", id="fb-hint")

    def on_mount(self) -> None:
        self._load_dir(self._cwd)
        self._update_hint()

    # ------------------------------------------------------------------
    # Directory loading
    # ------------------------------------------------------------------

    def _load_dir(self, path: Path) -> None:
        self._cwd = path.resolve()
        try:
            children = sorted(self._cwd.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            children = []

        dirs = [p for p in children if p.is_dir() and not p.name.startswith(".")]
        files = [p for p in children if p.is_file() and not p.name.startswith(".")]
        self._entries = dirs + files
        self.cursor = 0
        self._render_header()
        self._render_list()

    def _render_header(self) -> None:
        self.query_one("#fb-header", Static).update(
            Text(str(self._cwd), style=_HEADER_COLOR)
        )

    def _update_hint(self) -> None:
        self.query_one("#fb-hint", Static).update(
            "←: parent  →: enter dir  enter: select file  home: reset  esc: cancel"
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_list(self) -> None:
        if not self._entries:
            self.query_one("#fb-list", Static).update(
                Text("(empty directory)", style=_DIM)
            )
            return

        text = Text()
        for i, entry in enumerate(self._entries):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            is_dir = entry.is_dir()

            if is_selected and self.has_focus:
                style = f"bold {_CURSOR_FOCUSED}"
                marker = "► "
            elif is_selected:
                style = f"bold {_CURSOR_UNFOCUSED}"
                marker = "► "
            else:
                style = _DIR_COLOR if is_dir else _FILE_COLOR
                marker = "  "

            name = f"/{entry.name}" if is_dir else entry.name
            text.append(marker, style=style)
            text.append(name, style=style)

        self.query_one("#fb-list", Static).update(text)

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_cursor(self) -> None:
        self._render_list()
        self._scroll_cursor_visible()

    def _scroll_cursor_visible(self) -> None:
        self.call_after_refresh(self._do_scroll_cursor_visible)

    def _do_scroll_cursor_visible(self) -> None:
        scroll = self.query_one("#fb-list-scroll", VerticalScroll)
        if scroll.size.height == 0:
            return
        cursor_top = self.cursor
        cursor_bottom = cursor_top + 1
        if cursor_top < scroll.scroll_y:
            scroll.scroll_y = cursor_top
        elif cursor_bottom > scroll.scroll_y + scroll.size.height:
            scroll.scroll_y = cursor_bottom - scroll.size.height

    # ------------------------------------------------------------------
    # Focus changes
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        self._render_list()

    def on_blur(self) -> None:
        self._render_list()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self._entries and self.cursor > 0:
            self.cursor -= 1

    def action_cursor_down(self) -> None:
        if self._entries and self.cursor < len(self._entries) - 1:
            self.cursor += 1

    def action_go_parent(self) -> None:
        parent = self._cwd.parent
        if parent != self._cwd:
            old_name = self._cwd.name
            self._load_dir(parent)
            # Try to place cursor on the directory we came from
            for i, entry in enumerate(self._entries):
                if entry.name == old_name:
                    self.cursor = i
                    break

    def action_go_home(self) -> None:
        if self._cwd != self._home:
            self._load_dir(self._home)

    def action_enter_dir(self) -> None:
        if not self._entries:
            return
        entry = self._entries[min(self.cursor, len(self._entries) - 1)]
        if entry.is_dir():
            self._load_dir(entry)

    def action_select(self) -> None:
        if not self._entries:
            return
        entry = self._entries[min(self.cursor, len(self._entries) - 1)]
        if entry.is_dir():
            self._load_dir(entry)
        else:
            self.post_message(self.FileSelected(entry))

    def action_dismiss(self) -> None:
        self.post_message(self.Dismissed())
