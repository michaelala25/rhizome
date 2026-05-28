"""Logging pane widget — displays a rolling window of log messages."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import RichLog, Static


class LogsStatusBar(Static):
    """Static hint bar displayed at the bottom of the logs pane."""

    DEFAULT_CSS = """
    LogsStatusBar {
        height: 3;
        dock: bottom;
        background: rgb(12, 12, 12);
        color: rgb(100, 100, 100);
        padding: 0 1;
        border-top: solid rgb(60, 60, 60);
    }
    """

    def render(self):
        return "ctrl+g to open in editor"


class _SelectableRichLog(RichLog):
    """RichLog subclass that adds text selection support.

    RichLog is missing three things that the newer ``Log`` widget has:
    1. ``apply_offsets()`` in ``render_line`` (for coordinate resolution)
    2. ``get_selection()`` (for extracting selected text)
    3. ``selection_updated()`` / highlight rendering (for visual feedback)
    """

    def _get_line_text(self, y: int) -> str:
        """Extract plain text for a rendered line."""
        if y >= len(self.lines):
            return ""
        return "".join(seg.text for seg in self.lines[y]._segments)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(self._get_line_text(y) for y in range(len(self.lines)))
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._line_cache.clear()
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y
        width = self.scrollable_content_region.width

        if content_y >= len(self.lines):
            return Strip.blank(width, self.rich_style)

        line = self.lines[content_y].crop_extend(scroll_x, scroll_x + width, self.rich_style)

        # Apply selection highlighting
        selection = self.text_selection
        if selection is not None:
            if (span := selection.get_span(content_y)) is not None:
                start, end = span
                line_text = Text.assemble(
                    *[(seg.text, seg.style) for seg in line._segments]
                )
                if end == -1:
                    end = len(line_text)
                selection_style = self.screen.get_component_rich_style(
                    "screen--selection"
                )
                line_text.stylize(selection_style, start, end)
                segments = list(line_text.render(self.app.console))
                line = Strip(segments, line.cell_length)

        line = line.apply_offsets(scroll_x, content_y)
        strip = line.apply_style(self.rich_style)
        return strip


class LoggingPane(Widget):
    """Displays log messages captured by the app's ``TUILogHandler``.

    Pull-based: polls the handler's deque on a 100ms interval and writes
    only the lines that haven't been synced yet.
    """

    BINDINGS = [
        Binding("ctrl+g", "open_logs_in_editor", "Open logs in editor", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    LoggingPane {
        height: 1fr;
        background: rgb(12, 12, 12);
    }
    LoggingPane #log-output {
        height: 1fr;
        background: rgb(12, 12, 12) !important;
        background-tint: initial !important;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._synced_count: int = 0

    def compose(self) -> ComposeResult:
        yield _SelectableRichLog(max_lines=2000, markup=True, wrap=True, auto_scroll=True, id="log-output")
        yield LogsStatusBar()

    def on_mount(self) -> None:
        handler = getattr(self.app, "tui_log_handler", None)
        if handler is None:
            return
        self._sync(handler)
        self._poll_timer = self.set_interval(0.1, self._poll)

    def _poll(self) -> None:
        handler = getattr(self.app, "tui_log_handler", None)
        if handler is not None and handler.total_count > self._synced_count:
            # Skip syncing when the pane isn't visible — the width is 0
            # and RichLog wraps to min_width (78), producing spurious
            # line breaks.  Lines will catch up on the next visible poll.
            rich_log = self.query_one("#log-output", RichLog)
            if rich_log.scrollable_content_region.width <= 0:
                return
            self._sync(handler)

    def _sync(self, handler) -> None:
        """Write any unsynced lines from the handler's deque to the RichLog."""
        behind = handler.total_count - self._synced_count
        rich_log = self.query_one("#log-output", RichLog)
        if behind > len(handler.lines):
            # Deque wrapped — some lines were dropped; full replay
            rich_log.clear()
            for line in handler.lines:
                rich_log.write(line)
        else:
            start = len(handler.lines) - behind
            for i in range(start, len(handler.lines)):
                rich_log.write(handler.lines[i])
        self._synced_count = handler.total_count

    def action_open_logs_in_editor(self) -> None:
        """Dump current log buffer to a temp file and open it in $EDITOR."""
        handler = getattr(self.app, "tui_log_handler", None)
        if handler is None or not handler.lines:
            return

        _markup_re = re.compile(r"\[/?[^\]]*\]")
        plain_lines = [_markup_re.sub("", line) for line in handler.lines]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix="rhizome-logs-", delete=False
        ) as tmp:
            tmp.write("\n".join(plain_lines) + "\n")
            tmp_path = tmp.name

        editor = os.environ.get("EDITOR", "nano")
        try:
            with self.app.suspend():
                subprocess.run([editor, tmp_path])
        finally:
            os.unlink(tmp_path)
