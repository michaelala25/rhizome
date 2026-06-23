"""ChatArea view — the conversation panel for the rewritten chat stack.

Renders a ``ChatAreaModel``: a ``VerticalScroll`` feed (one widget per feed entry, mounted into per-node
``DepthWrapper`` rules that draw the branch-depth guides), a ``ChatInput`` bound to ``vm.chat_input``, a
``CommandPalette`` bound to ``vm.command_palette``, and a docked ``StatusBar`` bound to ``vm.status_bar``.
Feed-entry widgets are dispatched by runtime type through the shared feed-view registry.

The status bar is a fixed element of the chat area (not a swappable workspace panel). Mode/verbosity
cycling (shift+tab / ctrl+b) is wired here — the view owns the cycle order and calls the VM's setters.
Commit mode isn't wired yet; it slots in as the VM grows. Branch navigation is handled by the focused
``BranchPoint`` widgets themselves, not by this view.

Focus: ChatArea is a ``FocusOrchestrationMixin`` over its navigable feed items plus the chat input.
``on_focus`` (and so any external ``focus()`` — mount, tab switch) delegates inward to ``focus_first``,
landing on the chat input when enabled or the pending interrupt otherwise; ctrl+up/down step through the
graph (see ``_get_focus_graph``).
"""

from __future__ import annotations

from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget

from rhizome.agent.app_context import VALID_VERBOSITIES
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.chat_area.conversation_graph import ConversationItem, ConversationNode, Cursor
from rhizome.tui.keybindings import Keybind
from rhizome.tui.types import Mode
from rhizome.tui.widgets.chat_pane.chat_input import ChatInput
from rhizome.tui.widgets.chat_pane.command_palette import CommandPalette
from rhizome.tui.widgets.chat_area.status import StatusBar
from rhizome.tui.widgets.shared.focus_orchestration import Direction, FocusGraph, FocusOrchestrationMixin
from rhizome.tui.widgets.view_base import ViewBase
# Feed dispatch: import the manifest for its registry side effect (see feed_views.py / feed_registry.py).
from rhizome.tui.widgets.chat_area import feed_views  # noqa: F401
from rhizome.tui.widgets.chat_pane.feed_registry import view_for


# Mode cycle order for shift+tab (idle → learn → review → idle). The view owns this rotation; the VM
# only records the resulting mode. The verbosity cycle reads its vocabulary from ``VALID_VERBOSITIES``.
_MODE_CYCLE: tuple[Mode, ...] = (Mode.IDLE, Mode.LEARN, Mode.REVIEW)


class DepthWrapper(Vertical):
    """Per-node container for one conversation-graph node's feed entries.

    Draws a single left ``│`` rule (``border-left``); nesting a depth-D wrapper inside a depth-(D-1) one
    gives y-position-aware branch guides — the rule spans exactly that node's content, no coordinate math.
    Left border only (zero padding / margin / right border) so content stays flush with the parent's right
    edge regardless of depth.
    """


class ChatArea(ViewBase[ChatAreaModel], FocusOrchestrationMixin):

    # Focusable so external ``focus()`` (mount, tab switch, vm RequestFocus) lands here and the mixin's
    # ``on_focus`` delegates inward to ``focus_first``. Set explicitly: ``ViewBase``'s own (Widget-default)
    # ``can_focus`` would otherwise win MRO over the mixin's default.
    can_focus = True

    BINDINGS = [
        # ctrl+c: copy a selection if there is one (standard terminal behaviour), else cancel the
        # current branch's in-flight run. Not commit-aware yet (no commit mode in the VM).
        Keybind.ChatCancel.as_binding("cancel", "Cancel", show=False),
        # shift+tab / ctrl+b: cycle the checked-out branch's mode / verbosity. Verbosity is priority so
        # it fires while the chat input holds focus (ctrl+b would otherwise be a cursor move there).
        Keybind.ChatCycleMode.as_binding("cycle_mode", "Cycle mode", show=False),
        Keybind.ChatCycleVerbosity.as_binding("cycle_verbosity", "Cycle verbosity", show=False, priority=True),
        # ctrl+up / ctrl+down: step focus across navigable feed items and the chat input (the focus
        # graph built in ``_get_focus_graph``). Not priority — they bubble up from the focused input.
        Keybind.ChatNavUp.  as_binding("focus_neighbour('up')",   show=False),
        Keybind.ChatNavDown.as_binding("focus_neighbour('down')", show=False),
    ]

    DEFAULT_CSS = """
    ChatArea {
        layout: vertical;
        height: 1fr;
    }
    ChatArea #message-area {
        height: 1fr;
        background: $surface-darken-1;
        padding: 1;
        scrollbar-color: rgb(60, 60, 60);
        scrollbar-color-hover: rgb(80, 80, 80);
        scrollbar-color-active: rgb(100, 100, 100);
        /* Let the inner content exceed the viewport so a narrow pane surfaces a horizontal scrollbar
         * (``#message-area-inner`` keeps its 100-cell floor). */
        overflow-x: auto;
    }
    /* Width-floored wrapper between the scroll viewport and the feed widgets / DepthWrapper chain, so
     * there's one element to pin ``min-width`` on (depth-0 feed entries live here directly — no rule on
     * the outermost level). */
    ChatArea #message-area-inner {
        width: 100%;
        height: auto;
        min-width: 100;
    }
    ChatArea #chat-input {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
    }
    ChatArea #chat-input.--shell-mode,
    ChatArea #chat-input.--shell-mode:focus {
        border: tall rgb(200, 60, 60);
    }
    ChatArea CommandPalette {
        background: rgb(12, 12, 12);
    }
    /* Per-depth wrapper: ``border-left`` only — no padding/margin/right border — so the content area at
     * every depth extends flush to the right edge. Each nested level consumes one LEFT cell for the rule. */
    ChatArea DepthWrapper {
        height: auto;
        width: 100%;
        padding: 0;
        margin: 0;
        border-left: solid rgb(60, 60, 60);
    }
    """

    def __init__(self, vm: ChatAreaModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)

        # Mounted feed widgets keyed by ConversationItem.id — addressed by id, not position, because the
        # feed mutates mid-stream (items appended after the agent's open segment; the thinking indicator
        # removed) without disturbing surrounding positions.
        self._mounted: dict[int, Widget] = {}

        # Per-node DepthWrapper keyed by node id. The root's "wrapper" is ``#message-area-inner`` itself
        # (no rule on the outermost level); deeper nodes get a real DepthWrapper, created on demand.
        self._depth_wrappers: dict[int, Widget] = {}

        self._vm.subscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.subscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.subscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.subscribe(self._vm.Callbacks.OnCursorMoved, self._on_cursor_moved)
        self._vm.subscribe(self._vm.Callbacks.OnInterruptChanged, self._on_interrupt_changed)
        self._vm.subscribe(self._vm.Callbacks.OnHint, self._on_hint)

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.unsubscribe(self._vm.Callbacks.OnCursorMoved, self._on_cursor_moved)
        self._vm.unsubscribe(self._vm.Callbacks.OnInterruptChanged, self._on_interrupt_changed)
        self._vm.unsubscribe(self._vm.Callbacks.OnHint, self._on_hint)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="message-area"):
            yield Vertical(id="message-area-inner")
        yield ChatInput(self._vm.chat_input, id="chat-input")
        yield CommandPalette(self._vm.command_palette, id="command-palette")
        yield StatusBar(self._vm.status_bar, id="status-bar")

    def on_mount(self) -> None:
        # Inject Textual's worker scheduler so the graph spawns run tasks here (the VM late-binds it).
        self._vm.set_worker_scheduler(self.run_worker)
        # Render whatever the VM already holds visible (the workspace may have built it before mount).
        self._reconcile(self._vm.cursor)
        # Route initial focus once the inner widgets exist (focus_first → chat input if enabled, else the
        # pending interrupt). After-refresh because compose's children aren't mounted yet here.
        self.call_after_refresh(self.focus_first)

    # ------------------------------------------------------------------
    # Feed rendering helpers
    # ------------------------------------------------------------------

    def _node_ids(self, cursor: Cursor) -> tuple[int, ...]:
        return tuple(node.id for node in cursor.nodes())

    def _segments(self, cursor: Cursor) -> list[tuple[ConversationNode, list[ConversationItem]]]:
        return self._vm.conversation_graph.feed_segments(cursor)

    @staticmethod
    def _feed_node_id(item_id: int) -> str:
        """Widget id (and focus-graph node id) for a feed item — item ids are globally unique."""
        return f"feed-item-{item_id}"

    def _build_entry_widget(self, item: ConversationItem) -> Widget:
        view_cls = view_for(item.entry)
        if view_cls is None:
            raise TypeError(f"No view registered for feed entry type: {type(item.entry).__name__}")
        # The id lets the focus graph resolve this item by ``query_one`` (see ``_get_focus_graph``).
        return view_cls(item.entry, id=self._feed_node_id(item.id))

    def _container_for_node(self, node_id: int, cursor: Cursor) -> Widget:
        """The widget owning ``node_id``'s feed entries. Depth-0 (root) lives in ``#message-area-inner``;
        deeper nodes live in their own DepthWrapper."""
        if node_id == self._node_ids(cursor)[0]:
            return self.query_one("#message-area-inner", Vertical)
        return self._depth_wrappers[node_id]

    def _ensure_wrapper_chain(self, node_ids: tuple[int, ...], cursor: Cursor) -> None:
        """Create a DepthWrapper for each non-root node on the path, nested into its parent's container."""
        for i in range(1, len(node_ids)):
            node_id = node_ids[i]
            if node_id in self._depth_wrappers:
                continue
            wrapper = DepthWrapper()
            self._depth_wrappers[node_id] = wrapper
            self._container_for_node(node_ids[i - 1], cursor).mount(wrapper)

    def _mount_item(self, node_id: int, item: ConversationItem, node_ids: tuple[int, ...], cursor: Cursor) -> None:
        """Mount ``item``'s widget into ``node_id``'s container, before any deeper wrapper already there.

        Items in a non-leaf node's feed (e.g. a branch indicator appended just before the cursor descends)
        must mount *above* the deeper wrapper holding their subtree — otherwise they'd land beneath their
        own subtree's rule. On the leaf there's no deeper wrapper, so the plain append is correct.
        """
        widget = self._build_entry_widget(item)
        container = self._container_for_node(node_id, cursor)
        idx = node_ids.index(node_id)
        deeper = self._depth_wrappers.get(node_ids[idx + 1]) if idx + 1 < len(node_ids) else None
        if deeper is not None and deeper in container.children:
            container.mount(widget, before=deeper)
        else:
            container.mount(widget)
        self._mounted[item.id] = widget

    def _scroll_end(self) -> None:
        self.query_one("#message-area", VerticalScroll).scroll_end(animate=False)

    # ------------------------------------------------------------------
    # VM → view callbacks
    # ------------------------------------------------------------------

    def _on_feed_append(self, node: ConversationNode, item: ConversationItem) -> None:
        cursor = self._vm.cursor
        node_ids = self._node_ids(cursor)
        if node.id not in node_ids:
            return  # appended into a pinned, non-visible branch; surfaces on the next cursor move
        self._ensure_wrapper_chain(node_ids, cursor)
        self._mount_item(node.id, item, node_ids, cursor)
        self._scroll_end()

    def _on_feed_remove(self, node: ConversationNode, item: ConversationItem) -> None:
        widget = self._mounted.pop(item.id, None)
        if widget is not None:
            widget.remove()

    def _on_feed_clear(self, node: ConversationNode) -> None:
        cursor = self._vm.cursor
        if node.id not in self._node_ids(cursor):
            return
        container = self._container_for_node(node.id, cursor)
        for fid in [fid for fid, w in self._mounted.items() if w.parent is container]:
            self._mounted.pop(fid).remove()

    def _on_cursor_moved(self, cursor: Cursor) -> None:
        self._reconcile(cursor)

    def _reconcile(self, cursor: Cursor) -> None:
        """Diff mounted widgets + wrappers against the visible feed for ``cursor``.

        By ``ConversationItem.id`` for entries and node id for wrappers. Stale wrappers (nodes no longer on
        the path) drop wholesale, their children cascading off the tree; surviving wrappers stay put, so the
        longest shared-ancestor chain keeps any in-flight view state (e.g. an ``AgentMessage`` drain task).
        """
        node_ids = self._node_ids(cursor)
        node_id_set = set(node_ids)
        segments = self._segments(cursor)
        live_ids = {item.id for _, items in segments for item in items}

        for stale_node in [nid for nid in self._depth_wrappers if nid not in node_id_set]:
            self._depth_wrappers.pop(stale_node).remove()
        for stale_id in [mid for mid in self._mounted if mid not in live_ids]:
            self._mounted.pop(stale_id).remove()

        self._ensure_wrapper_chain(node_ids, cursor)
        for node, items in segments:
            for item in items:
                if item.id in self._mounted:
                    continue
                self._mount_item(node.id, item, node_ids, cursor)
        self._scroll_end()

    def _on_interrupt_changed(self, node: ConversationNode) -> None:
        # Input is locked while an interrupt is pending on the *visible* branch; off-path interrupts
        # don't touch it (a cursor move re-derives the lockout in _on_cursor_moved).
        if node is not self._vm.cursor.node:
            return
        resolved = node.pending_interrupt is None
        if resolved:
            # Interrupt cleared on the visible branch: return focus to the input (the now-inert interrupt
            # widget would otherwise keep it). This is the *only* place an enable refocuses — branch
            # navigation re-enables run through _on_cursor_moved and deliberately leave focus put.
            self.focus_first()

    def _on_hint(self, msg: str) -> None:
        self.app.notify(msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)
            return
        if self._vm.agent_busy():
            self._vm.cancel()

    def action_cycle_mode(self) -> None:
        """shift+tab: advance idle → learn → review → idle. Silent — a quick toggle reflected in the
        status bar, not a chat-visible event. The VM just records the resulting mode."""
        nxt = _MODE_CYCLE[(_MODE_CYCLE.index(self._vm.mode) + 1) % len(_MODE_CYCLE)]
        self._vm.set_mode(nxt, silent=True)

    def action_cycle_verbosity(self) -> None:
        """ctrl+b: advance through the answer-verbosity vocabulary, wrapping."""
        cur = self._vm.verbosity
        idx = VALID_VERBOSITIES.index(cur) if cur in VALID_VERBOSITIES else 0
        self._vm.set_verbosity(VALID_VERBOSITIES[(idx + 1) % len(VALID_VERBOSITIES)])

    def action_focus_neighbour(self, direction: Direction) -> None:
        if self.focus_neighbour(direction) is None:
            raise SkipAction()

    # ------------------------------------------------------------------
    # Focus orchestration (FocusOrchestrationMixin seams)
    # ------------------------------------------------------------------

    def _navigable_node_ids(self) -> list[str]:
        """Mounted, navigable feed items in visible (top→bottom) order, as focus-graph node ids."""
        ids: list[str] = []
        for _node, items in self._segments(self._vm.cursor):
            for item in items:
                if item.entry.is_navigable and item.id in self._mounted:
                    ids.append(self._feed_node_id(item.id))
        return ids

    def _get_focus_graph(self) -> FocusGraph:
        """Vertical chain over the navigable feed items, anchored at the chat input below them.

        From a feed item: up steps to the previous (clamps at the top — no up edge on the first), down
        steps to the next or, past the last, lands on the chat input. From the input: ctrl+up enters the
        feed at the bottom-most item, ctrl+down at the top-most.

        ``source`` (where external ``focus()`` lands, via ``focus_first``) is the chat input when it's
        enabled, else the message area — when gated by a pending interrupt, focus rests on the feed
        scroll container rather than the disabled input.
        """
        nav = self._navigable_node_ids()
        edges: dict[str, dict[Direction, str]] = {}
        for i, node_id in enumerate(nav):
            edge: dict[Direction, str] = {}
            if i > 0:
                edge["up"] = nav[i - 1]
            edge["down"] = nav[i + 1] if i + 1 < len(nav) else "chat-input"
            edges[node_id] = edge
        edges["chat-input"] = {"up": nav[-1], "down": nav[0]} if nav else {}

        if self._vm.chat_input.enabled:
            source = "chat-input"
        else:
            source = "message-area"
        return FocusGraph(source=source, edges=edges)

    def _is_node_available(self, node_id: str) -> bool:
        # Don't route focus to the input while it's gated (a pending interrupt owns input).
        if node_id == "chat-input":
            return self._vm.chat_input.enabled
        return True
