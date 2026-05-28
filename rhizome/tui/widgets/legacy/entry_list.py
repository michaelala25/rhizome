"""EntryList — read-only widget for browsing knowledge entries."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rhizome.db import KnowledgeEntry

# Shared color constants (also used by CommitProposal)
ENTRY_DIM = "rgb(100,100,100)"
ENTRY_HINT = "rgb(80,80,80)"
ENTRY_ACCENT = "rgb(255,80,80)"
_FOCUS_GREEN = "rgb(100,200,100)"
_ALT_GREY = "rgb(180,180,180)"


class EntryList(Widget, can_focus=True):
    """Read-only entry list with detail panel for browsing KnowledgeEntry objects."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "dismiss", show=False),
        Binding("escape", "dismiss", show=False),
    ]

    DEFAULT_CSS = """
    EntryList {
        height: auto;
        layout: vertical;
        padding: 0 1;
    }
    EntryList #ev-entry-list-scroll {
        height: auto;
        max-height: 10;
        margin: 1 0 1 0;
    }
    EntryList #ev-entry-list {
        height: auto;
    }
    EntryList #ev-detail-panel {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    EntryList #ev-title {
        text-style: bold;
        margin-bottom: 0;
    }
    EntryList #ev-meta {
        color: rgb(100,100,100);
        margin: 0 0 1 0;
    }
    EntryList #ev-content-scroll {
        height: auto;
        max-height: 10;
    }
    EntryList #ev-content {
        height: auto;
    }
    EntryList #ev-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 1;
    }
    EntryList.--compact {
        padding: 0 0 0 1;
    }
    EntryList.--compact #ev-detail-panel {
        display: none;
    }
    EntryList.--compact #ev-entry-list-scroll {
        margin: 1 0 0 0;
        max-height: 30;
    }
    EntryList.--compact #ev-compact-hint {
        display: block;
    }
    EntryList #ev-compact-hint {
        display: none;
        color: rgb(80,80,80);
        margin: 1 0 0 1;
    }
    """

    class Dismissed(Message):
        """Posted when the user presses Escape to leave the entry viewer."""

    class CursorChanged(Message):
        """Posted when the cursor moves to a different entry."""

        def __init__(self, entry: KnowledgeEntry | None) -> None:
            super().__init__()
            self.entry = entry

    cursor: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list[KnowledgeEntry] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="ev-empty")
        with VerticalScroll(id="ev-entry-list-scroll"):
            yield Static(id="ev-entry-list")
        yield Static("", id="ev-compact-hint")
        with Vertical(id="ev-detail-panel"):
            yield Static(id="ev-title")
            yield Static(id="ev-meta")
            with VerticalScroll(id="ev-content-scroll"):
                yield Static(id="ev-content")

    def on_mount(self) -> None:
        self._apply_empty_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_entries(self, entries: list[KnowledgeEntry]) -> None:
        """Replace the displayed entries and reset the cursor."""
        self._entries = list(entries)
        self.cursor = 0
        self._apply_empty_state()
        if self._entries:
            self._render_entry_list()
            if not self.has_class("--compact"):
                self._render_detail()
            self._scroll_cursor_visible()
            self.post_message(self.CursorChanged(self._entries[0]))
        else:
            self.post_message(self.CursorChanged(None))

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_cursor(self) -> None:
        if self._entries:
            self._render_entry_list()
            if self.has_class("--compact"):
                self._render_compact_hint()
            else:
                self._render_detail()
            self._scroll_cursor_visible()
            entry = self._entries[min(self.cursor, len(self._entries) - 1)]
            self.post_message(self.CursorChanged(entry))
        else:
            self.post_message(self.CursorChanged(None))

    def _scroll_cursor_visible(self) -> None:
        """Scroll the entry list so the cursor row is visible (deferred to after layout)."""
        self.call_after_refresh(self._do_scroll_cursor_visible)

    def _do_scroll_cursor_visible(self) -> None:
        scroll = self.query_one("#ev-entry-list-scroll", VerticalScroll)
        if scroll.size.height == 0:
            return
        # Each entry is one line; use line height to compute pixel offset
        line_height = 1
        cursor_top = self.cursor * line_height
        cursor_bottom = cursor_top + line_height
        if cursor_top < scroll.scroll_y:
            scroll.scroll_y = cursor_top
        elif cursor_bottom > scroll.scroll_y + scroll.size.height:
            scroll.scroll_y = cursor_bottom - scroll.size.height

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _apply_empty_state(self) -> None:
        """Toggle between empty message and list/detail views."""
        empty = not self._entries
        compact = self.has_class("--compact")
        self.query_one("#ev-empty", Static).display = empty
        self.query_one("#ev-entry-list-scroll", VerticalScroll).display = not empty
        # In compact mode, detail panel is always hidden
        self.query_one("#ev-detail-panel", Vertical).display = not empty and not compact
        if empty:
            self.query_one("#ev-empty", Static).update("(No entries for this topic)")
            self.query_one("#ev-compact-hint", Static).update("")

    def _render_entry_list(self) -> None:
        compact = self.has_class("--compact")

        if compact:
            self._render_entry_list_compact()
            return

        num_width = len(str(len(self._entries))) + 2  # "N. "
        title_widths = [len(e.title) for e in self._entries]
        max_title = max(title_widths, default=0)

        right_parts = [
            (e.entry_type.value if e.entry_type else "—") for e in self._entries
        ]
        max_right = max((len(r) for r in right_parts), default=0)

        text = Text()
        for i, entry in enumerate(self._entries):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            marker = "► " if is_selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            title = entry.title

            if is_selected and self.has_focus:
                style = f"bold {_FOCUS_GREEN}"
                marker_style = f"bold {_FOCUS_GREEN}"
                right_style = ENTRY_DIM
            elif is_selected:
                style = "bold"
                marker_style = "bold"
                right_style = ENTRY_DIM
            else:
                style = "" if i % 2 == 0 else _ALT_GREY
                marker_style = ""
                right_style = ENTRY_DIM

            text.append(marker, style=marker_style)
            text.append(num, style=style)
            text.append(title, style=style)

            right = right_parts[i].rjust(max_right)
            padding = max_title - len(title) + 2
            gap = " " * padding
            text.append(gap)
            text.append(right, style=right_style)

        self.query_one("#ev-entry-list", Static).update(text)

    def _render_entry_list_compact(self) -> None:
        """Render a minimal entry list for compact (3-pane) mode."""
        text = Text()
        for i, entry in enumerate(self._entries):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            if is_selected and self.has_focus:
                style = f"bold {_FOCUS_GREEN}"
            elif is_selected:
                style = "bold"
            else:
                style = "" if i % 2 == 0 else _ALT_GREY

            marker = "► " if is_selected else "  "
            text.append(marker, style=style)
            text.append(entry.title, style=style)

        self.query_one("#ev-entry-list", Static).update(text)

        # Update compact metadata hint
        self._render_compact_hint()

    def _render_compact_hint(self) -> None:
        """Update the metadata hint shown below the list in compact mode."""
        if not self._entries:
            self.query_one("#ev-compact-hint", Static).update("")
            return
        idx = min(self.cursor, len(self._entries) - 1)
        entry = self._entries[idx]
        parts: list[str] = []
        if entry.entry_type:
            parts.append(entry.entry_type.value)
        if entry.created_at is not None:
            parts.append(f"created {entry.created_at:%Y-%m-%d}")
        self.query_one("#ev-compact-hint", Static).update("  ".join(parts))

    def _render_detail(self) -> None:
        if not self._entries:
            return
        idx = min(self.cursor, len(self._entries) - 1)
        entry = self._entries[idx]

        panel = self.query_one("#ev-detail-panel", Vertical)
        panel.border_title = f"Entry {idx + 1}"

        self.query_one("#ev-title", Static).update(entry.title)

        # Meta line
        etype = entry.entry_type.value if entry.entry_type else "—"
        parts = [f"Type: {etype}"]
        if entry.difficulty is not None:
            parts.append(f"Difficulty: {entry.difficulty}")
        if entry.created_at is not None:
            parts.append(f"Created: {entry.created_at:%Y-%m-%d}")
        self.query_one("#ev-meta", Static).update("  ".join(parts))

        self.query_one("#ev-content", Static).update(entry.content)

    # ------------------------------------------------------------------
    # Focus changes
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        if self._entries:
            self.call_after_refresh(self._render_entry_list)

    def on_blur(self) -> None:
        if self._entries:
            self.call_after_refresh(self._render_entry_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self._entries and self.cursor > 0:
            self.cursor -= 1

    def action_cursor_down(self) -> None:
        if self._entries and self.cursor < len(self._entries) - 1:
            self.cursor += 1

    def action_dismiss(self) -> None:
        self.post_message(self.Dismissed())
