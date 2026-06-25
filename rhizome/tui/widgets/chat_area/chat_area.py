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

from textual import on
from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.widget import Widget

from rhizome.agent.app_context import VALID_VERBOSITIES
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.chat_area.conversation_graph import ConversationItem, ConversationNode, Cursor
from rhizome.tui.keybindings import Keybind
from rhizome.tui.types import Mode
from rhizome.tui.widgets.chat_area.chat_input import ChatInput
from rhizome.tui.widgets.chat_area.command_palette import CommandPalette
from rhizome.tui.widgets.options_editor import OptionsEditor
from rhizome.tui.widgets.browser import Browser
from rhizome.tui.widgets.chat_area.status import StatusBar
from rhizome.tui.widgets.shared.focus_orchestration import Direction, FocusGraph, FocusOrchestrationMixin
from rhizome.tui.widgets.view_base import ViewBase
# Feed dispatch: import the manifest for its registry side effect (see feed_views.py / feed_registry.py).
from rhizome.tui.widgets.chat_area import feed_views  # noqa: F401
from rhizome.tui.widgets.chat_area.feed_registry import view_for


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
        # Commit mode (gated by ``check_action`` — inert, and falling through, outside commit mode). Plain
        # up/down walk the message-only commit graph; space toggles the focused message; ctrl+j submits;
        # esc exits. up/down + ctrl+j + esc are priority so they beat the scroll container / the focused
        # input, then ``check_action`` returns None to let the key fall through when commit isn't the owner.
        Keybind.ChatCommitNavUp.  as_binding("focus_message_neighbour('up')",   show=False, priority=True),
        Keybind.ChatCommitNavDown.as_binding("focus_message_neighbour('down')", show=False, priority=True),
        Keybind.ChatCommitToggle.as_binding("commit_toggle", show=False),
        Keybind.ChatCommitCancel.as_binding("commit_cancel", show=False, priority=True),
        Keybind.ChatCommitSubmit.as_binding("commit_submit", show=False, priority=True),
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

        # Commit mode: the feed-node id of the last-focused selectable message. It is the entry point the
        # main (ctrl) graph re-enters the message cluster at, and the cluster's lone representative there —
        # so the same message lives in both the main graph and the message-only commit graph (it's the
        # "sticky" one). ``None`` until a message is focused / no message exists.
        self._commit_entry_id: str | None = None

        self._vm.subscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.subscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.subscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.subscribe(self._vm.Callbacks.OnCursorMoved, self._on_cursor_moved)
        self._vm.subscribe(self._vm.Callbacks.OnInterruptChanged, self._on_interrupt_changed)
        self._vm.subscribe(self._vm.Callbacks.OnVisibilityChanged, self._on_visibility_changed)
        self._vm.subscribe(self._vm.Callbacks.OnHint, self._on_hint)
        self._vm.subscribe(self._vm.Callbacks.OnCommitModeChanged, self._on_commit_mode_changed)
        self._vm.subscribe(self._vm.Callbacks.OnCommitSelectionChanged, self._on_commit_selection_changed)

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.unsubscribe(self._vm.Callbacks.OnCursorMoved, self._on_cursor_moved)
        self._vm.unsubscribe(self._vm.Callbacks.OnInterruptChanged, self._on_interrupt_changed)
        self._vm.unsubscribe(self._vm.Callbacks.OnVisibilityChanged, self._on_visibility_changed)
        self._vm.unsubscribe(self._vm.Callbacks.OnHint, self._on_hint)
        self._vm.unsubscribe(self._vm.Callbacks.OnCommitModeChanged, self._on_commit_mode_changed)
        self._vm.unsubscribe(self._vm.Callbacks.OnCommitSelectionChanged, self._on_commit_selection_changed)

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

    def _mount_item(
        self,
        node_id: int,
        item: ConversationItem,
        node_ids: tuple[int, ...],
        cursor: Cursor,
        *,
        before: Widget | None = None,
    ) -> None:
        """Mount ``item``'s widget into ``node_id``'s container at the right position.

        ``before`` is the next already-mounted sibling to land in front of — passed by ``_reconcile`` so an
        item un-hidden mid-feed (a thinking segment revealed by ``ShowThinking``) slots into order instead
        of at the container tail. When ``None`` (a fresh in-order mount / a live append), the item goes
        before this node's deeper wrapper if one exists, else appends: items in a non-leaf node's feed
        (e.g. a branch indicator) must mount *above* the deeper wrapper holding their subtree, or they'd
        land beneath their own subtree's rule. On the leaf there's no deeper wrapper, so append is correct.
        """
        widget = self._build_entry_widget(item)
        container = self._container_for_node(node_id, cursor)
        if before is None:
            idx = node_ids.index(node_id)
            deeper = self._depth_wrappers.get(node_ids[idx + 1]) if idx + 1 < len(node_ids) else None
            if deeper is not None and deeper in container.children:
                before = deeper
        container.mount(widget, before=before)
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
        if not self._vm.is_visible(item.entry):
            return  # hidden by a display filter (e.g. thinking off); a toggle reconciles it back in
        self._ensure_wrapper_chain(node_ids, cursor)
        self._mount_item(node.id, item, node_ids, cursor)
        self._scroll_end()

    def _on_feed_remove(self, node: ConversationNode, item: ConversationItem) -> None:
        widget = self._mounted.pop(item.id, None)
        if widget is not None:
            widget.remove()

    @on(OptionsEditor.Dismissed)
    def _on_options_editor_dismissed(self, event: OptionsEditor.Dismissed) -> None:
        self._dismiss_feed_widget(event.control)

    @on(Browser.Dismissed)
    def _on_browser_dismissed(self, event: Browser.Dismissed) -> None:
        self._dismiss_feed_widget(event.control)

    def _dismiss_feed_widget(self, widget: Widget) -> None:
        """Drop a self-dismissing feed widget and return focus to the input — dismissal means
        "done with this side-task, back to typing." Removal flows through the VM (``remove_item``
        → ``OnFeedRemoved`` → ``_on_feed_remove`` unmounts the widget)."""
        for fid, mounted in self._mounted.items():
            if mounted is widget:
                self._vm.remove_item(fid)
                self.focus_first()
                return

    def _on_feed_clear(self, node: ConversationNode) -> None:
        cursor = self._vm.cursor
        if node.id not in self._node_ids(cursor):
            return
        container = self._container_for_node(node.id, cursor)
        for fid in [fid for fid, w in self._mounted.items() if w.parent is container]:
            self._mounted.pop(fid).remove()

    def _on_cursor_moved(self, cursor: Cursor) -> None:
        self._reconcile(cursor)

    def _on_visibility_changed(self) -> None:
        # A display filter toggled (e.g. ShowThinking): reconcile mounts/unmounts only the now-(in)visible
        # entries — a keyed diff, so the rest of the feed and the current focus are left untouched.
        self._reconcile(self._vm.cursor)

    def _reconcile(self, cursor: Cursor) -> None:
        """Diff mounted widgets + wrappers against the visible feed for ``cursor``.

        By ``ConversationItem.id`` for entries and node id for wrappers. Stale wrappers (nodes no longer on
        the path) drop wholesale, their children cascading off the tree; surviving wrappers stay put, so the
        longest shared-ancestor chain keeps any in-flight view state (e.g. an ``AgentMessage`` drain task).
        """
        node_ids = self._node_ids(cursor)
        node_id_set = set(node_ids)
        segments = self._segments(cursor)
        # The live set honours the display filters (is_visible): an item the filter now hides counts as
        # stale and is torn down below; one it now reveals is absent from ``_mounted`` and mounts back in.
        live_ids = {item.id for _, items in segments for item in items if self._vm.is_visible(item.entry)}

        for stale_node in [nid for nid in self._depth_wrappers if nid not in node_id_set]:
            self._depth_wrappers.pop(stale_node).remove()
        for stale_id in [mid for mid in self._mounted if mid not in live_ids]:
            self._mounted.pop(stale_id).remove()

        self._ensure_wrapper_chain(node_ids, cursor)
        for node, items in segments:
            for idx, item in enumerate(items):
                if item.id in self._mounted or item.id not in live_ids:
                    continue
                # Anchor before the next already-mounted sibling so a revealed-mid-feed item lands in
                # order rather than at the tail; None ⇒ the deeper-wrapper / append fallback in _mount_item.
                after = next((it for it in items[idx + 1:] if it.id in self._mounted), None)
                before = self._mounted[after.id] if after is not None else None
                self._mount_item(node.id, item, node_ids, cursor, before=before)
        self._scroll_end()

        # Repaint commit decoration for the now-visible branch (after refresh, so freshly-mounted widgets
        # have composed their checkbox child) and re-anchor the entry message onto it.
        if self._vm.commit_active:
            self.call_after_refresh(self._refresh_commit_decoration)

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
    # Commit mode (view-side selection + focus)
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate the commit-mode bindings. Returning ``None`` (not ``False``) makes Textual treat the
        binding as absent, so the keystroke propagates normally — a text-cursor move in the input,
        scrolling at a message boundary, the root-level space toggle — whenever commit mode isn't the
        owner of that key. Plain up/down + space act only with a message focused; submit/cancel anywhere."""
        if action in ("focus_message_neighbour", "commit_toggle"):
            return True if self._vm.commit_active and self._focused_node_item() is not None else None
        if action in ("commit_submit", "commit_cancel"):
            return True if self._vm.commit_active else None
        return super().check_action(action, parameters)

    def action_focus_message_neighbour(self, direction: Direction) -> None:
        """Plain up/down in commit mode: step the message-only commit graph. At a boundary the move
        fails and we ``SkipAction`` so the keystroke falls through to the scroll container."""
        if self.focus_neighbour(direction, graph=self._commit_focus_graph()) is None:
            raise SkipAction()

    def action_commit_toggle(self) -> None:
        target = self._focused_node_item()
        if target is None:
            raise SkipAction()
        self._vm.toggle_commit_selection(*target)

    def action_commit_submit(self) -> None:
        self._vm.submit_commit(self._vm.chat_input.buffer)

    def action_commit_cancel(self) -> None:
        self._vm.exit_commit_mode()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """Track the last-focused selectable message — the point the main graph re-enters the cluster at.
        The focused message is the one that lives in both graphs (the "sticky" one)."""
        if self._vm.commit_active and self._focused_node_item() is not None:
            self._commit_entry_id = event.widget.id

    def _on_commit_mode_changed(self, active: bool) -> None:
        if not active:
            self._clear_commit_decoration()
            self._commit_entry_id = None
            self.focus_first()
            return
        # Entering: decorate the visible selectable messages, then drop focus onto the entry message
        # (re-anchored to the bottom-most one in _refresh_commit_decoration) so up/down + space work at once.
        self._refresh_commit_decoration()
        widget = self._mounted.get(self._item_id_from_node(self._commit_entry_id))
        if widget is not None:
            widget.focus()
        else:
            self.focus_first()

    def _on_commit_selection_changed(
        self, node: ConversationNode, item: ConversationItem, staged: bool
    ) -> None:
        widget = self._mounted.get(item.id)
        if widget is not None and hasattr(widget, "set_commit_decoration"):
            widget.set_commit_decoration(selectable=True, selected=staged)

    def _refresh_commit_decoration(self) -> None:
        """Paint commit decoration on every visible selectable message (selectable + its staged state from
        the VM's global selection) and make them focusable. Re-anchors the entry message onto the
        now-visible branch, since a cursor move remounts a different feed."""
        message_ids = self._commit_message_ids()
        if self._commit_entry_id not in message_ids:
            self._commit_entry_id = message_ids[-1] if message_ids else None
        for _node, items in self._segments(self._vm.cursor):
            for item in items:
                widget = self._mounted.get(item.id)
                if widget is None or not hasattr(widget, "set_commit_decoration"):
                    continue
                if self._vm.is_commit_selectable(item.entry):
                    widget.set_commit_decoration(selectable=True, selected=self._vm.is_committed(item.id))
                    widget.can_focus = True

    def _clear_commit_decoration(self) -> None:
        for widget in self._mounted.values():
            if hasattr(widget, "set_commit_decoration"):
                widget.set_commit_decoration(selectable=False, selected=False)
                widget.can_focus = False

    def _commit_message_ids(self) -> list[str]:
        """Selectable, mounted message node ids in visible (top→bottom) order — the commit graph's nodes."""
        ids: list[str] = []
        for _node, items in self._segments(self._vm.cursor):
            for item in items:
                if item.id in self._mounted and self._vm.is_commit_selectable(item.entry):
                    ids.append(self._feed_node_id(item.id))
        return ids

    def _commit_focus_graph(self) -> FocusGraph:
        """Vertical chain over the selectable messages only — the graph plain up/down traverses."""
        ids = self._commit_message_ids()
        edges: dict[str, dict[Direction, str]] = {}
        for i, node_id in enumerate(ids):
            edge: dict[Direction, str] = {}
            if i > 0:
                edge["up"] = ids[i - 1]
            if i + 1 < len(ids):
                edge["down"] = ids[i + 1]
            edges[node_id] = edge
        source = self._commit_entry_id if self._commit_entry_id in ids else (ids[-1] if ids else "")
        return FocusGraph(source=source, edges=edges)

    def _focused_node_item(self) -> tuple[ConversationNode, ConversationItem] | None:
        """(node, item) for the currently-focused selectable message, or None when focus is elsewhere."""
        focused = self.screen.focused
        if focused is None or focused.id is None:
            return None
        item_id = self._item_id_from_node(focused.id)
        if item_id is None:
            return None
        for node, items in self._segments(self._vm.cursor):
            for item in items:
                if item.id == item_id and self._vm.is_commit_selectable(item.entry):
                    return node, item
        return None

    @staticmethod
    def _item_id_from_node(node_id: str | None) -> int | None:
        prefix = "feed-item-"
        if node_id is None or not node_id.startswith(prefix):
            return None
        try:
            return int(node_id[len(prefix):])
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Focus orchestration (FocusOrchestrationMixin seams)
    # ------------------------------------------------------------------

    def _navigable_node_ids(self) -> list[str]:
        """Mounted, navigable feed items in visible (top→bottom) order, as focus-graph node ids.

        In commit mode the messages are navigated by the separate commit focus graph (plain up/down), so
        the main graph (ctrl+up/down) carries only the *one* entry message as the message cluster's lone
        representative — ctrl-nav steps over the remaining widgets and re-enters the cluster there."""
        commit = self._vm.commit_active
        ids: list[str] = []
        for _node, items in self._segments(self._vm.cursor):
            for item in items:
                if item.id not in self._mounted:
                    continue
                node_id = self._feed_node_id(item.id)
                if item.entry.is_navigable:
                    ids.append(node_id)
                elif commit and node_id == self._commit_entry_id and self._vm.is_commit_selectable(item.entry):
                    ids.append(node_id)
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
