"""ExplorerViewer — unified browser for topics, entries, and flashcards."""

from __future__ import annotations

import enum

from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Static, Tree
from textual.widgets._tree import TreeNode

from rhizome.db import Flashcard, KnowledgeEntry, Topic
from rhizome.db.operations import (
    count_entries,
    list_entries,
    list_flashcards_by_entries,
    list_flashcards_by_topic,
)

from rhizome.tui.types import DatabaseCommitted

from .entry_list import EntryList
from .flashcard_list import FlashcardList
from .messages import ActiveTopicChanged
from .navigable import NavigableWidgetMixin
from .topic_tree import TopicTree


class ViewMode(enum.IntEnum):
    TOPICS = 0
    TOPICS_ENTRIES = 1
    TOPICS_FLASHCARDS = 2
    TOPICS_ENTRIES_FLASHCARDS = 3


_MODE_LABELS = {
    ViewMode.TOPICS: "topics",
    ViewMode.TOPICS_ENTRIES: "topics & entries",
    ViewMode.TOPICS_FLASHCARDS: "topics & flashcards",
    ViewMode.TOPICS_ENTRIES_FLASHCARDS: "topics, entries & flashcards",
}

# Ordered list of pane IDs per mode (for ctrl+left/right focus cycling).
_MODE_PANES: dict[ViewMode, list[str]] = {
    ViewMode.TOPICS: ["explorer-tree-pane"],
    ViewMode.TOPICS_ENTRIES: ["explorer-tree-pane", "explorer-entry-pane"],
    ViewMode.TOPICS_FLASHCARDS: ["explorer-tree-pane", "explorer-flashcard-pane"],
    ViewMode.TOPICS_ENTRIES_FLASHCARDS: [
        "explorer-tree-pane",
        "explorer-entry-pane",
        "explorer-flashcard-pane",
    ],
}

_CSS_CLASSES = {
    ViewMode.TOPICS: None,
    ViewMode.TOPICS_ENTRIES: "--show-entries",
    ViewMode.TOPICS_FLASHCARDS: "--show-flashcards",
    ViewMode.TOPICS_ENTRIES_FLASHCARDS: "--show-all",
}


class ExplorerViewer(NavigableWidgetMixin, Vertical):
    """A bordered container for browsing topics, entries, and flashcards."""

    DEFAULT_CSS = """
    ExplorerViewer {
        height: auto;
        margin-top: 1;
        padding: 0 0 1 1;
    }
    ExplorerViewer #explorer-split {
        height: auto;
    }
    ExplorerViewer #explorer-tree-pane {
        width: 1fr;
        height: auto;
    }
    ExplorerViewer #explorer-help {
        color: $text-muted;
        margin: 1 0 0 1;
    }
    ExplorerViewer #explorer-tree-scroll {
        height: auto;
        overflow-x: auto;
        overflow-y: hidden;
        margin-top: 1;
    }
    ExplorerViewer TopicTree {
        height: auto;
        width: auto;
        scrollbar-size: 0 0;
        padding-left: 2;
        margin-bottom: 1;
        background: transparent;
    }
    ExplorerViewer TopicTree:focus > .tree--cursor {
        background: transparent;
        color: rgb(255,80,80);
        text-style: bold;
    }
    ExplorerViewer TopicTree > .tree--cursor {
        background: transparent;
        color: rgb(180,60,60);
        text-style: bold;
    }
    ExplorerViewer #explorer-count-hint {
        color: $text-muted;
        margin: 0 0 0 3;
    }
    ExplorerViewer .pane-title {
        display: none;
        text-style: bold;
        color: $text-muted;
        margin: 1 0 0 1;
    }
    ExplorerViewer #explorer-entry-pane {
        display: none;
        height: auto;
    }
    ExplorerViewer #explorer-flashcard-pane {
        display: none;
        height: auto;
    }
    ExplorerViewer #explorer-dismiss {
        dock: right;
        width: 3;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: $text-muted;
        margin: 0;
        padding: 0;
    }
    ExplorerViewer #explorer-dismiss:hover {
        color: $error;
    }

    /* -- Mode: topics & entries -- */
    ExplorerViewer.--show-entries {
        height: auto;
    }
    ExplorerViewer.--show-entries #explorer-split {
        height: auto;
    }
    ExplorerViewer.--show-entries #explorer-tree-pane {
        width: 30%;
    }
    ExplorerViewer.--show-entries #explorer-entry-pane {
        display: block;
        width: 70%;
        height: auto;
    }
    ExplorerViewer.--show-entries #explorer-count-hint {
        display: none;
    }
    ExplorerViewer.--show-entries .pane-title {
        display: block;
    }

    /* -- Mode: topics & flashcards -- */
    ExplorerViewer.--show-flashcards {
        height: auto;
    }
    ExplorerViewer.--show-flashcards #explorer-split {
        height: auto;
    }
    ExplorerViewer.--show-flashcards #explorer-tree-pane {
        width: 30%;
    }
    ExplorerViewer.--show-flashcards #explorer-flashcard-pane {
        display: block;
        width: 70%;
        height: auto;
    }
    ExplorerViewer.--show-flashcards #explorer-count-hint {
        display: none;
    }
    ExplorerViewer.--show-flashcards .pane-title {
        display: block;
    }

    /* -- Mode: topics, entries & flashcards -- */
    ExplorerViewer.--show-all {
        height: auto;
    }
    ExplorerViewer.--show-all #explorer-split {
        height: auto;
    }
    ExplorerViewer.--show-all #explorer-tree-pane {
        width: 20%;
    }
    ExplorerViewer.--show-all #explorer-entry-pane {
        display: block;
        width: 35%;
        height: auto;
    }
    ExplorerViewer.--show-all #explorer-flashcard-pane {
        display: block;
        width: 45%;
        height: auto;
    }
    ExplorerViewer.--show-all #explorer-count-hint {
        display: none;
    }
    ExplorerViewer.--show-all .pane-title {
        display: block;
    }
    """

    BINDINGS = [
        Binding("tab", "cycle_mode", show=False),
        Binding("ctrl+left", "focus_prev_pane", show=False),
        Binding("ctrl+right", "focus_next_pane", show=False),
        Binding("ctrl+j", "select_topic", show=False),
        Binding("escape", "dismiss_viewer", show=False),
    ]

    class Dismissed(Message):
        """Posted when the user dismisses the explorer."""

    view_mode: reactive[ViewMode] = reactive(ViewMode.TOPICS)

    def __init__(self, session_factory=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session_factory = session_factory
        # Entry data/cursor caches (keyed by topic_id, shared across all entry-showing modes)
        self._entry_cache: dict[int, list[KnowledgeEntry]] = {}
        self._entry_count_cache: dict[int, int] = {}
        self._entry_cursor_cache: dict[int, int] = {}
        # Flashcard data/cursor caches (keyed by topic_id, for two-pane "topics & flashcards" mode)
        self._fc_by_topic_cache: dict[int, list[Flashcard]] = {}
        self._fc_count_cache: dict[int, int] = {}
        self._fc_cursor_by_topic_cache: dict[int, int] = {}
        # Flashcard data cache (keyed by entry_id, for three-pane mode — no cursor persistence)
        self._fc_by_entry_cache: dict[int, list[Flashcard]] = {}
        self._current_topic_id: int | None = None

    def compose(self):
        yield Button("x", id="explorer-dismiss")
        yield Static("", id="explorer-help")
        with Horizontal(id="explorer-split"):
            with Vertical(id="explorer-tree-pane"):
                yield Static("Topics", classes="pane-title")
                with ScrollableContainer(id="explorer-tree-scroll"):
                    yield TopicTree(self._session_factory)
                yield Static("", id="explorer-count-hint")
            with Vertical(id="explorer-entry-pane"):
                yield Static("Entries", classes="pane-title")
                yield EntryList(id="explorer-entry-viewer")
            with Vertical(id="explorer-flashcard-pane"):
                yield Static("Flashcards", classes="pane-title")
                yield FlashcardList(id="explorer-flashcard-viewer")

    def on_mount(self) -> None:
        self.setup_navigation()
        self.border_title = "Explore"
        self._update_help_text()

    # ------------------------------------------------------------------
    # Data refresh (called when DB state changes externally)
    # ------------------------------------------------------------------

    async def notify_database_committed(self, event: DatabaseCommitted) -> None:
        tables = event.changed_tables

        if not tables:
            # Unknown change — full refresh
            self._entry_cache.clear()
            self._entry_count_cache.clear()
            self._fc_by_topic_cache.clear()
            self._fc_count_cache.clear()
            self._fc_by_entry_cache.clear()
            tree = self.query_one(TopicTree)
            await tree.invalidate_and_refresh()
            await self._load_data_for_current_topic()
            return

        refreshed_tree = False
        if tables & {"topic"}:
            tree = self.query_one(TopicTree)
            await tree.invalidate_and_refresh()
            refreshed_tree = True

        if tables & {"knowledge_entry", "tag", "knowledge_entry_tag"}:
            self._entry_cache.clear()
            self._entry_count_cache.clear()

        if tables & {"flashcard"}:
            self._fc_by_topic_cache.clear()
            self._fc_count_cache.clear()
            self._fc_by_entry_cache.clear()

        # Reload the current topic's data if any relevant cache was cleared
        if tables & {"topic", "knowledge_entry", "tag", "knowledge_entry_tag", "flashcard"}:
            if not refreshed_tree:
                # Topic tree is fine, just reload the data panes
                await self._load_data_for_current_topic()

    # ------------------------------------------------------------------
    # Help text
    # ------------------------------------------------------------------

    def _update_help_text(self) -> None:
        parts = ["tab: cycle view"]
        if self.view_mode != ViewMode.TOPICS:
            parts.append("ctrl+\u2190/\u2192: switch pane")
        parts.append("ctrl+enter: select topic")
        parts.append("esc: dismiss")
        self.query_one("#explorer-help", Static).update("  ".join(parts))

    # ------------------------------------------------------------------
    # View mode cycling
    # ------------------------------------------------------------------

    def watch_view_mode(self, old_value: ViewMode, new_value: ViewMode) -> None:
        # Save cursors from the old mode before switching
        if self._current_topic_id is not None:
            entry_viewer = self.query_one("#explorer-entry-viewer", EntryList)
            self._entry_cursor_cache[self._current_topic_id] = entry_viewer.cursor
            if old_value == ViewMode.TOPICS_FLASHCARDS:
                fc_viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
                self._fc_cursor_by_topic_cache[self._current_topic_id] = fc_viewer.cursor

        # Remove old CSS class
        old_cls = _CSS_CLASSES.get(old_value)
        if old_cls:
            self.remove_class(old_cls)
        # Add new CSS class
        new_cls = _CSS_CLASSES.get(new_value)
        if new_cls:
            self.add_class(new_cls)

        # Toggle compact mode on entry viewer
        entry_viewer = self.query_one("#explorer-entry-viewer", EntryList)
        if new_value == ViewMode.TOPICS_ENTRIES_FLASHCARDS:
            entry_viewer.add_class("--compact")
        else:
            entry_viewer.remove_class("--compact")

        self._update_help_text()

        # Ensure the tree pane has focus after mode change
        self.query_one(TopicTree).focus()

        # Load data for current topic in the new mode
        self.call_after_refresh(self._load_data_for_current_topic)
        self.call_after_refresh(self.scroll_visible)

    def action_cycle_mode(self) -> None:
        next_val = (self.view_mode + 1) % len(ViewMode)
        self.view_mode = ViewMode(next_val)

    # ------------------------------------------------------------------
    # Pane focus navigation (ctrl+left / ctrl+right)
    # ------------------------------------------------------------------

    def _get_focused_pane_index(self) -> int:
        """Return the index of the currently focused pane, or 0."""
        panes = _MODE_PANES[self.view_mode]
        for i, pane_id in enumerate(panes):
            widget = self.query_one(f"#{pane_id}")
            # Check if the focused widget is inside this pane
            focused = self.screen.focused
            if focused is not None:
                if focused is widget or widget in focused.ancestors_with_self:
                    return i
        return 0

    def _focus_pane(self, pane_id: str) -> None:
        """Focus the appropriate child within a pane."""
        if pane_id == "explorer-tree-pane":
            self.query_one(TopicTree).focus()
        elif pane_id == "explorer-entry-pane":
            viewer = self.query_one("#explorer-entry-viewer", EntryList)
            if viewer._entries:
                viewer.focus()
        elif pane_id == "explorer-flashcard-pane":
            viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
            if viewer._flashcards:
                viewer.focus()

    def action_focus_next_pane(self) -> None:
        panes = _MODE_PANES[self.view_mode]
        if len(panes) <= 1:
            return
        idx = (self._get_focused_pane_index() + 1) % len(panes)
        self._focus_pane(panes[idx])

    def action_focus_prev_pane(self) -> None:
        panes = _MODE_PANES[self.view_mode]
        if len(panes) <= 1:
            return
        idx = (self._get_focused_pane_index() - 1) % len(panes)
        self._focus_pane(panes[idx])

    # ------------------------------------------------------------------
    # Horizontal scroll to keep highlighted node visible
    # ------------------------------------------------------------------

    def _scroll_to_node(self, node: TreeNode[Topic]) -> None:
        scroll = self.query_one("#explorer-tree-scroll", ScrollableContainer)
        depth = 0
        current = node
        tree = self.query_one(TopicTree)
        while current.parent is not None and current is not tree.root:
            depth += 1
            current = current.parent
        indent = depth * tree.guide_depth
        label_len = len(str(node.label))
        container_width = scroll.size.width
        node_left = max(indent - 4, 0)
        node_right = indent + label_len + 4
        if node_left >= scroll.scroll_x and node_right <= scroll.scroll_x + container_width:
            return
        scroll.scroll_x = node_left

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------

    async def _load_data_for_current_topic(self) -> None:
        """Load entries/flashcards for the currently highlighted topic."""
        tree = self.query_one(TopicTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        await self._load_for_topic(node.data)

    async def _load_for_topic(self, topic: Topic) -> None:
        """Load and display data for a topic based on the current view mode."""
        session_factory = self._session_factory
        mode = self.view_mode

        # Entries (needed for TOPICS_ENTRIES and TOPICS_ENTRIES_FLASHCARDS)
        if mode in (ViewMode.TOPICS_ENTRIES, ViewMode.TOPICS_ENTRIES_FLASHCARDS):
            if topic.id not in self._entry_cache:
                async with session_factory() as session:
                    entries = await list_entries(session, topic.id)
                    self._entry_cache[topic.id] = entries
                    self._entry_count_cache[topic.id] = len(entries)
            entry_viewer = self.query_one("#explorer-entry-viewer", EntryList)
            entry_viewer.set_entries(self._entry_cache[topic.id])
            # Restore persisted entry cursor (shared across all entry-showing modes)
            if topic.id in self._entry_cursor_cache:
                entry_viewer.cursor = min(
                    self._entry_cursor_cache[topic.id],
                    max(len(self._entry_cache[topic.id]) - 1, 0),
                )
            entry_viewer._scroll_cursor_visible()

        # Flashcards by topic (two-pane mode only — persists cursor per topic)
        if mode == ViewMode.TOPICS_FLASHCARDS:
            if topic.id not in self._fc_by_topic_cache:
                async with session_factory() as session:
                    flashcards = await list_flashcards_by_topic(session, topic.id)
                    self._fc_by_topic_cache[topic.id] = flashcards
                    self._fc_count_cache[topic.id] = len(flashcards)
            fc_viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
            fc_viewer.set_flashcards(self._fc_by_topic_cache[topic.id])
            if topic.id in self._fc_cursor_by_topic_cache:
                fc_viewer.cursor = min(
                    self._fc_cursor_by_topic_cache[topic.id],
                    max(len(self._fc_by_topic_cache[topic.id]) - 1, 0),
                )
            fc_viewer._scroll_cursor_visible()

        # Three-pane mode: flashcard panel starts empty, reset on each topic change
        if mode == ViewMode.TOPICS_ENTRIES_FLASHCARDS:
            fc_viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
            fc_viewer.set_flashcards([])

        # Entry count hint (for TOPICS mode only)
        if mode == ViewMode.TOPICS:
            if topic.id not in self._entry_count_cache:
                async with session_factory() as session:
                    count = await count_entries(session, topic.id)
                    self._entry_count_cache[topic.id] = count

        self._update_count_hint(topic.id)
        self.call_after_refresh(self.scroll_visible)

    def _update_count_hint(self, topic_id: int) -> None:
        """Update the count hint below the tree."""
        parts: list[str] = []
        entry_count = self._entry_count_cache.get(topic_id)
        if entry_count is not None:
            if entry_count == 0:
                parts.append("no entries")
            elif entry_count == 1:
                parts.append("1 entry")
            else:
                parts.append(f"{entry_count} entries")

        fc_count = self._fc_count_cache.get(topic_id)
        if fc_count is not None:
            if fc_count == 0:
                parts.append("no flashcards")
            elif fc_count == 1:
                parts.append("1 flashcard")
            else:
                parts.append(f"{fc_count} flashcards")

        if parts:
            hint = f"({', '.join(parts)} in this topic)"
        else:
            hint = ""
        self.query_one("#explorer-count-hint", Static).update(hint)

    # ------------------------------------------------------------------
    # Topic highlight — load data when cursor moves in the tree
    # ------------------------------------------------------------------

    async def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[Topic]) -> None:
        topic = event.node.data
        if topic is None:
            return
        self._scroll_to_node(event.node)

        # Save cursor positions for the previous topic
        if self._current_topic_id is not None:
            # Entry cursor persists across all entry-showing modes
            entry_viewer = self.query_one("#explorer-entry-viewer", EntryList)
            self._entry_cursor_cache[self._current_topic_id] = entry_viewer.cursor
            # Flashcard cursor persists only in two-pane mode
            if self.view_mode == ViewMode.TOPICS_FLASHCARDS:
                fc_viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
                self._fc_cursor_by_topic_cache[self._current_topic_id] = fc_viewer.cursor
        self._current_topic_id = topic.id

        await self._load_for_topic(topic)

    # ------------------------------------------------------------------
    # Entry cursor change — load flashcards for selected entry (mode 4)
    # ------------------------------------------------------------------

    async def on_entry_list_cursor_changed(self, event: EntryList.CursorChanged) -> None:
        if self.view_mode != ViewMode.TOPICS_ENTRIES_FLASHCARDS:
            return
        event.stop()

        fc_viewer = self.query_one("#explorer-flashcard-viewer", FlashcardList)
        if event.entry is None:
            fc_viewer.set_flashcards([])
            return

        entry_id = event.entry.id
        if entry_id not in self._fc_by_entry_cache:
            session_factory = self._session_factory
            async with session_factory() as session:
                flashcards = await list_flashcards_by_entries(session, [entry_id])
                self._fc_by_entry_cache[entry_id] = flashcards
        # Always reset to first flashcard (no per-entry cursor persistence)
        fc_viewer.set_flashcards(self._fc_by_entry_cache[entry_id])

    # ------------------------------------------------------------------
    # Topic selection (Enter / Ctrl+J)
    # ------------------------------------------------------------------

    def on_tree_node_selected(self, event: Tree.NodeSelected[Topic]) -> None:
        if event.node.data is None:
            return
        event.stop()
        self._post_topic_selected(event.node)

    def action_select_topic(self) -> None:
        """Ctrl+J — select the currently highlighted topic and exit."""
        tree = self.query_one(TopicTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            self._post_topic_selected(node)

    def _post_topic_selected(self, node: TreeNode[Topic]) -> None:
        path: list[str] = []
        current = node
        while current.parent is not None:
            if current.data is not None:
                path.append(current.data.name)
            current = current.parent
        path.reverse()
        self.deactivate_navigation()
        self.post_message(ActiveTopicChanged(node.data, path))

    # ------------------------------------------------------------------
    # Child viewer dismissed — return focus to appropriate pane
    # ------------------------------------------------------------------

    def on_entry_list_dismissed(self, event: EntryList.Dismissed) -> None:
        event.stop()
        self.query_one(TopicTree).focus()

    def on_flashcard_list_dismissed(self, event: FlashcardList.Dismissed) -> None:
        event.stop()
        if self.view_mode == ViewMode.TOPICS_ENTRIES_FLASHCARDS:
            # In 3-pane mode, go back to entry viewer
            entry_viewer = self.query_one("#explorer-entry-viewer", EntryList)
            if entry_viewer._entries:
                entry_viewer.focus()
            else:
                self.query_one(TopicTree).focus()
        else:
            self.query_one(TopicTree).focus()

    # ------------------------------------------------------------------
    # Dismiss viewer (Escape from tree)
    # ------------------------------------------------------------------

    def action_dismiss_viewer(self) -> None:
        self.deactivate_navigation()
        self.post_message(self.Dismissed())

    # ------------------------------------------------------------------
    # Dismiss button
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "explorer-dismiss":
            self.deactivate_navigation()
            self.post_message(self.Dismissed())

    # ------------------------------------------------------------------
    # Focus delegation
    # ------------------------------------------------------------------

    def focus(self, scroll_visible: bool = True) -> None:
        self.query_one(TopicTree).focus(scroll_visible)
