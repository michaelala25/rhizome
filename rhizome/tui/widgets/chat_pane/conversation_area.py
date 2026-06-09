"""ConversationArea view — the conversation half of the chat pane.

Renders a ``ConversationAreaModel``: a ``VerticalScroll`` feed (one widget per feed entry, mounted
into per-node ``DepthWrapper`` rules), a ``ChatInput`` bound to ``vm.chat_input``, a
``CommandPalette`` bound to ``vm.command_palette``, and a ``StatusBar``. All input-area keystroke
handling (Enter, Tab, Up, Down, Escape, Ctrl+Enter) lives inside ``ChatInput`` itself.

Workspace actions the conversation can't perform itself — open/close tabs, quit, toggle the
resource viewer, rename the enclosing tab — arrive from the VM as ``OnNotification`` /
``OnTabRenamed``. This view handles the conversation-local notifications (transient toasts) and
forwards the rest up to the orchestrator ``ChatPane`` as ``WorkspaceAction`` / ``TabRenamed``
Textual messages.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.chat_pane.chat_input import ChatInput
from rhizome.tui.widgets.chat_pane.command_palette import CommandPalette
from rhizome.tui.widgets.chat_pane.status import StatusBar
from rhizome.tui.widgets.options_editor import OptionsEditor
from rhizome.app.chat_pane.conversation_graph import NodeId
from rhizome.app.chat_pane.conversation_area import ConversationAreaModel
# Feed dispatch: ``feed_views`` is imported for its side effect — it populates the registry that
# ``view_for`` reads. Without that import every lookup would return ``None`` (see feed_registry.py).
from rhizome.tui.widgets.chat_pane import feed_views  # noqa: F401
from rhizome.tui.widgets.chat_pane.feed_registry import view_for


FeedEntryWidget = Widget


class DepthWrapper(Vertical):
    """Per-node container for feed entries belonging to one conversation-graph node.

    Each wrapper draws a single ``│`` rule on its left side (via ``border-left``); nesting a
    depth-``D`` wrapper inside a depth-``D-1`` wrapper gives the y-position-aware indentation
    guides — the rule for depth ``D`` spans exactly the y-range occupied by that node's content,
    no post-layout coordinate math needed.

    Critically uses ``border-left`` only and zero padding/margin/``border-right`` so the right
    edge of content stays flush with the parent's right edge regardless of nesting depth. Adding
    any of those would push content inward from the right on every nested level.
    """


class ConversationArea(ViewBase[ConversationAreaModel]):

    class WorkspaceAction(Message):
        """A workspace-level action the conversation can't perform itself (open/close tab, quit,
        toggle the resource viewer). Posted upward for the orchestrator ``ChatPane`` to carry out."""

        def __init__(self, action: "ConversationAreaModel.NotifyAction") -> None:
            super().__init__()
            self.action = action

    class TabRenamed(Message):
        """The agent renamed the chat session; the orchestrator relabels the enclosing tab."""

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    BINDINGS = [
        Keybind.ChatCycleMode.     as_binding("cycle_mode",      "Cycle mode",      show=False),
        Keybind.ChatCycleVerbosity.as_binding("cycle_verbosity", "Cycle verbosity", show=False, priority=True),
        # Commit-mode bindings. Guarded inside the action via ``check_action`` so they only fire
        # while ``state == COMMIT``. Priority handling:
        #   - up/down: priority so VerticalScroll's scroll bindings don't eat them when the
        #     message-area is focused. Trade-off: in commit mode, up/down in the chat input drive
        #     the cursor rather than history nav — acceptable since the input is just free-text
        #     instructions during commit.
        #   - space/enter/ctrl+j: no priority. When the input is focused it consumes them (typing /
        #     newline / submit-instructions); when the message-area is focused they bubble to the
        #     pane and toggle / submit.
        #   - ctrl+c: priority — always behaves the same regardless of focus.
        #   - ctrl+up / ctrl+down: state-dispatched feed nav (see the nav_up/nav_down bindings
        #     below); non-priority so the docked resource viewer can claim them when focused.
        Keybind.CursorUp.  as_binding("commit_cursor_up",   "Commit: cursor up",   show=False, priority=True),
        Keybind.CursorDown.as_binding("commit_cursor_down", "Commit: cursor down", show=False, priority=True),
        Keybind.Toggle.     as_binding("commit_toggle", "Commit: toggle", show=False),
        Keybind.MenuConfirm.as_binding("commit_toggle", "Commit: toggle", show=False),
        Keybind.ChatCommitSubmit.as_binding("commit_submit", "Commit: submit", show=False),
        # ctrl+c dispatches by state: copy selection → exit commit (in COMMIT) → abandon turn
        # (CONVERSATION + current branch busy). Lives on the pane, not commit-prefixed, so it
        # bypasses ``check_action``'s commit-only gate.
        Keybind.ChatCancel.as_binding("cancel", "Cancel", show=False, priority=False),
        # ctrl+up / ctrl+down do double duty depending on ``vm.state``: in COMMIT they flip focus
        # between the message-area cursor and the chat input (the legacy commit-mode behavior); in
        # CONVERSATION they walk the navigable feed entries (interrupts, branch indicators). Both
        # branches are dispatched from ``action_nav_up`` / ``action_nav_down``.
        #
        # NOT priority: nothing in the conversation subtree consumes ctrl+up/down (feed widgets leave
        # them free, the input's _on_key ignores them, DataTable/Tree/VerticalScroll don't bind them),
        # so they bubble here from any focus inside this view. The resource viewer (a sibling panel
        # hosted by the orchestrator) binds them for its own up/down half-switch when it's focused —
        # but its keystrokes bubble through the orchestrator, not here, so there's no contention.
        Keybind.ChatNavUp.  as_binding("nav_up",   "Navigate up",   show=False),
        Keybind.ChatNavDown.as_binding("nav_down", "Navigate down", show=False),
    ]

    DEFAULT_CSS = """
    ConversationArea {
        layout: vertical;
        height: 1fr;
    }
    ConversationArea #message-area {
        height: 1fr;
        background: $surface-darken-1;
        padding: 1;
        scrollbar-color: rgb(60, 60, 60);
        scrollbar-color-hover: rgb(80, 80, 80);
        scrollbar-color-active: rgb(100, 100, 100);
        /* Allow the inner content to be wider than the viewport — when the pane shrinks below the
         * feed's ``min-width``, ``#message-area-inner`` keeps its 100-cell floor and this scroll
         * container surfaces a horizontal scrollbar at its bottom edge (just above the input). */
        overflow-x: auto;
    }
    /* Wrapper that holds the feed. Lives between ``#message-area`` (the scroll container) and the
     * feed widgets / ``DepthWrapper`` chain so we have a single element to pin a width floor on.
     * Without it, the depth-0 children would inherit the viewport width and the horizontal scrollbar
     * would never trigger. */
    ConversationArea #message-area-inner {
        width: 100%;
        height: auto;
        min-width: 100;
    }
    ConversationArea #chat-input {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
    }
    ConversationArea #chat-input.--shell-mode,
    ConversationArea #chat-input.--shell-mode:focus {
        border: tall rgb(200, 60, 60);
    }
    ConversationArea CommandPalette {
        background: rgb(12, 12, 12);
    }
    /* Per-depth wrapper. ``border-left`` only — no padding, margin, or border-right — so the
     * content area at every depth extends flush to the right edge of #message-area. Each nested
     * level consumes exactly 1 cell on the LEFT for the rule; right-side geometry is untouched.
     */
    ConversationArea DepthWrapper {
        height: auto;
        width: 100%;
        padding: 0;
        margin: 0;
        border-left: solid rgb(60, 60, 60);
    }
    """

    def __init__(self, vm: ConversationAreaModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)

        # Mounted widgets keyed by FeedItem.id. The view addresses widgets by id (not position)
        # because the feed may be mutated mid-stream — items can be appended after the agent's open
        # segment, and later in the refactor the router will remove items (e.g. the thinking
        # indicator) without disturbing surrounding positions.
        self._mounted: dict[int, FeedEntryWidget] = {}

        # Per-node depth wrappers keyed by NodeId. Each wrapper holds the feed widgets for one node
        # on the cursor path; nesting matches the cursor's root-to-leaf order so the left-side
        # rules naturally span the y-range of their subtree. The root's "wrapper" is the
        # #message-area-inner container itself (not a DepthWrapper) — we don't want a rule on the
        # outermost level. Populated lazily on first feed_append/feed_replaced.
        self._depth_wrappers: dict[NodeId, Widget] = {}

        self._vm.subscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.subscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.subscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.subscribe(self._vm.Callbacks.OnFeedReplaced, self._on_feed_replaced)
        self._vm.subscribe(self._vm.Callbacks.OnNotification, self._on_notify)
        self._vm.subscribe(self._vm.Callbacks.OnTabRenamed, self._on_tab_rename)

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedAppended, self._on_feed_append)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedRemoved, self._on_feed_remove)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedCleared, self._on_feed_clear)
        self._vm.unsubscribe(self._vm.Callbacks.OnFeedReplaced, self._on_feed_replaced)
        self._vm.unsubscribe(self._vm.Callbacks.OnNotification, self._on_notify)
        self._vm.unsubscribe(self._vm.Callbacks.OnTabRenamed, self._on_tab_rename)

    def _on_notify(self, action: ConversationAreaModel.NotifyAction) -> None:
        # Forward-by-default: only the conversation-local toasts have a local handler here; anything
        # else is a workspace action the orchestrator owns, so post it upward.
        handler = self._NOTIFY_HANDLERS.get(action)
        if handler is None:
            self.post_message(self.WorkspaceAction(action))
            return
        handler(self)

    def _notify_agent_busy(self) -> None:
        self.app.notify(
            "Agent is thinking, you can submit after it completes or interrupt with Ctrl+C"
        )

    def _notify_hint_higher_verbosity(self) -> None:
        self.app.notify(
            "Hint: the agent has indicated that a higher verbosity "
            "may be required to properly answer your query."
        )

    def _notify_descend_required(self) -> None:
        self.app.notify(
            "You're sitting on a branch point. Click a branch indicator and descend "
            "(alt+↓) into one of the branches to continue."
        )

    def _on_tab_rename(self, name: str) -> None:
        # Tab identity is the orchestrator's concern — forward the new name up.
        self.post_message(self.TabRenamed(name))

    _NOTIFY_HANDLERS = {
        ConversationAreaModel.NotifyAction.AGENT_BUSY: _notify_agent_busy,
        ConversationAreaModel.NotifyAction.HINT_HIGHER_VERBOSITY: _notify_hint_higher_verbosity,
        ConversationAreaModel.NotifyAction.DESCEND_REQUIRED: _notify_descend_required,
    }

    def compose(self) -> ComposeResult:
        # ``#message-area`` is the scroll viewport; ``#message-area-inner`` carries the feed widgets
        # and the ``min-width: 100`` floor that drives horizontal scrolling on narrow panes.
        with VerticalScroll(id="message-area"):
            yield Vertical(id="message-area-inner")
        yield ChatInput(self._vm.chat_input, id="chat-input")
        yield CommandPalette(self._vm.command_palette, id="command-palette")
        yield StatusBar(self._vm.status_bar, id="status-bar")

    def on_mount(self) -> None:
        self._vm.set_worker_scheduler(self.run_worker)
        self._vm.bootstrap_agent_session(
            self.app.options,  # type: ignore[attr-defined]
            debug=getattr(self.app, "debug_logging", False),
        )
        self._vm.bootstrap_welcome(self.app.options)  # type: ignore[attr-defined]
        self.query_one("#chat-input", ChatInput).focus()

    # ------------------------------------------------------------------
    # VM → view callbacks
    # ------------------------------------------------------------------

    def _build_entry_widget(self, entry) -> FeedEntryWidget:
        """Dispatch a feed entry's runtime type to its concrete view via the feed-view registry."""
        view_cls = view_for(entry)
        if view_cls is None:
            raise TypeError(f"No view registered for feed entry type: {type(entry).__name__}")
        return view_cls(entry)

    def _container_for_node(self, node_id: NodeId) -> Widget:
        """Return the widget that owns feed entries for ``node_id``.

        Depth-0 (the root) lives directly in ``#message-area-inner`` (the width-floored wrapper
        inside the scroll viewport) so the outermost level has no rule. Deeper nodes live in their
        own ``DepthWrapper`` (created on demand by ``_ensure_wrapper_chain``).
        """
        cursor_path = self._vm._cursor.path
        if node_id == cursor_path[0]:
            return self.query_one("#message-area-inner", Vertical)
        return self._depth_wrappers[node_id]

    def _ensure_wrapper_chain(self, path: tuple[NodeId, ...]) -> None:
        """Make sure a DepthWrapper exists for each non-root node in ``path``, nested correctly.

        Each wrapper is mounted as the *last* child of its parent's container so that it visually
        appears below any feed items the parent already holds (including the branch indicator
        that introduced this depth). The "no items above the wrapper, all items below" structure
        is what the user picked when we discussed where the rule starts.
        """
        # path[0] = root; its container is #message-area, no DepthWrapper needed.
        for i in range(1, len(path)):
            node_id = path[i]
            if node_id in self._depth_wrappers:
                continue
            wrapper = DepthWrapper()
            self._depth_wrappers[node_id] = wrapper
            parent_container = self._container_for_node(path[i - 1])
            parent_container.mount(wrapper)

    def _find_item_node(self, item_id: int) -> NodeId | None:
        """Return the node id whose feed contains ``item_id``, or ``None`` if the item isn't on
        the cursor path (e.g. a pinned agent stream appending into a non-visible branch).
        """
        for node_id, items in self._vm.visible_feed_by_depth():
            if any(it.id == item_id for it in items):
                return node_id
        return None

    def _on_feed_append(self, item_id: int) -> None:
        node_id = self._find_item_node(item_id)
        if node_id is None:
            return  # appended to a pinned non-visible branch; surface on next cursor move
        # Ensure wrappers exist up to and including this item's depth.
        path = self._vm._cursor.path
        self._ensure_wrapper_chain(path)

        item = next(it for _, items in self._vm.visible_feed_by_depth() for it in items if it.id == item_id)
        widget = self._build_entry_widget(item.entry)
        container = self._container_for_node(node_id)

        # Items in a non-leaf node's feed (e.g. a branch indicator appended just before the cursor
        # descends) must mount *before* any deeper wrapper that's already a child of this
        # container — otherwise the new item would visually land beneath its own subtree's rule.
        # On the leaf node there's no deeper wrapper, so the plain append below is correct.
        cursor_path = path
        node_index = cursor_path.index(node_id)
        deeper_wrapper: Widget | None = None
        if node_index + 1 < len(cursor_path):
            deeper_wrapper = self._depth_wrappers.get(cursor_path[node_index + 1])

        if deeper_wrapper is not None and deeper_wrapper in container.children:
            container.mount(widget, before=deeper_wrapper)
        else:
            container.mount(widget)
        self._mounted[item_id] = widget
        self.query_one("#message-area", VerticalScroll).scroll_end(animate=False)

    def _on_feed_remove(self, item_id: int) -> None:
        widget = self._mounted.pop(item_id, None)
        if widget is not None:
            widget.remove()

    def _on_feed_clear(self) -> None:
        for widget in self._mounted.values():
            widget.remove()
        for wrapper in self._depth_wrappers.values():
            wrapper.remove()

        self._mounted.clear()
        self._depth_wrappers.clear()

    def _on_feed_replaced(self) -> None:
        """Reconcile mounted widgets + depth wrappers against the new cursor path.

        Diff is by ``FeedItem.id`` for entries and by ``NodeId`` for wrappers. Stale wrappers
        (nodes no longer on the cursor path) are removed wholesale; their child widgets cascade
        off the tree, so we also evict those ids from ``self._mounted``. Surviving wrappers stay
        put — common-prefix guarantee means the wrappers for the longest shared ancestor chain
        don't move, preserving any streaming view state (e.g. ``AgentMessage`` drain tasks)
        inside them.
        """
        new_path = self._vm._cursor.path
        new_path_set = set(new_path)
        new_ids = {item.id for _, items in self._vm.visible_feed_by_depth() for item in items}

        # Drop stale wrappers (and the items they contain).
        for stale_node in [nid for nid in self._depth_wrappers if nid not in new_path_set]:
            self._depth_wrappers.pop(stale_node).remove()
        # Drop stale items (those whose wrappers survived but whose item id is gone).
        for stale_id in [mid for mid in self._mounted if mid not in new_ids]:
            self._mounted.pop(stale_id).remove()

        # Make sure wrappers exist for every node on the new path.
        self._ensure_wrapper_chain(new_path)

        # Mount any newly-visible items into their correct depth's container.
        for node_id, items in self._vm.visible_feed_by_depth():
            container = self._container_for_node(node_id)
            for item in items:
                if item.id in self._mounted:
                    continue
                widget = self._build_entry_widget(item.entry)
                # See _on_feed_append for the before= rationale; same reason here.
                node_index = new_path.index(node_id)
                deeper_wrapper = (
                    self._depth_wrappers.get(new_path[node_index + 1])
                    if node_index + 1 < len(new_path)
                    else None
                )
                if deeper_wrapper is not None and deeper_wrapper in container.children:
                    container.mount(widget, before=deeper_wrapper)
                else:
                    container.mount(widget)
                self._mounted[item.id] = widget

        self.query_one("#message-area", VerticalScroll).scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_cycle_mode(self) -> None:
        await self._vm.cycle_mode()

    async def action_cycle_verbosity(self) -> None:
        await self._vm.cycle_verbosity()

    # ------------------------------------------------------------------
    # Commit-mode actions. ``check_action`` returns ``None`` to suppress the binding entirely when
    # the VM is not in COMMIT state, so up/down/enter etc. behave normally during conversations.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action.startswith("commit_"):
            if self._vm.state != ConversationAreaModel.State.COMMIT:
                return None
            # When the chat input is focused, up/down should drive the TextArea / history nav, not
            # the commit cursor. Returning None suppresses the priority binding so the keystroke
            # falls through to the input's _on_key. Toggle / submit / cancel / focus-flip bindings
            # remain active regardless of focus.
            if action in ("commit_cursor_up", "commit_cursor_down"):
                if self.query_one("#chat-input", ChatInput).has_focus:
                    return None
        return True

    def action_commit_cursor_up(self) -> None:
        self._vm.navigate_commit_cursor_up()

    def action_commit_cursor_down(self) -> None:
        self._vm.navigate_commit_cursor_down()

    def action_commit_toggle(self) -> None:
        self._vm.toggle_include_current_message_in_commit()

    def action_commit_submit(self) -> None:
        # ctrl+enter from the pane (input not focused) submits with empty instructions. ctrl+j sent
        # from the input is intercepted there as "insert newline" before this binding sees it.
        self._vm.submit_commit_payload("")

    def action_cancel(self) -> None:
        """ctrl+c dispatch — order matters:

        1. If there's selected text on screen, copy it (standard terminal behavior — most
           important for the user, do this first).
        2. In commit mode: exit commit mode.
        3. In conversation mode with the current branch's agent busy: cancel that turn.
        4. Otherwise: no-op.
        """
        selected = self.screen.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)
            return
        if self._vm.state == ConversationAreaModel.State.COMMIT:
            self._vm.exit_commit_mode()
            return
        if self._vm.agent_busy:
            self._vm.cancel_agent_turn()

    def action_nav_up(self) -> None:
        """ctrl+up dispatch. COMMIT → focus the message-area cursor (legacy commit-mode flip).
        CONVERSATION → step to the previous navigable feed entry (or jump to the bottom-most one
        when focus is on the chat input).
        """
        if self._vm.state == ConversationAreaModel.State.COMMIT:
            self._vm.request_focus()
            return
        self._vm.navigate_feed(-1, current_id=self._current_feed_id())

    def action_nav_down(self) -> None:
        """ctrl+down dispatch. COMMIT → focus the chat input (legacy commit-mode flip).
        CONVERSATION → step to the next navigable feed entry (or jump to the top-most one when
        focus is on the chat input).
        """
        if self._vm.state == ConversationAreaModel.State.COMMIT:
            self._vm.chat_input.request_focus()
            return
        self._vm.navigate_feed(+1, current_id=self._current_feed_id())

    def _current_feed_id(self) -> int | None:
        """Map the currently-focused widget back to a ``FeedItem.id`` via ``self._mounted``.

        Returns ``None`` if focus is on the chat input, the message-area scroll container, or any
        widget not descended from a mounted feed entry. The VM uses this to decide where to land
        next: a non-None id steps within the navigable list; ``None`` jumps to one end.
        """
        focused = self.screen.focused
        if focused is None:
            return None
        for fid, widget in self._mounted.items():
            if focused is widget or widget in focused.ancestors_with_self:
                return fid
        return None

    @on(OptionsEditor.Dismissed)
    def _on_options_editor_dismissed(self, event: OptionsEditor.Dismissed) -> None:
        """Drop the dismissing ``OptionsEditor`` from the feed and return focus to the chat
        input — the dismissal semantically means "I'm done with this side-task, back to
        typing." The footer's ``OptionsEditorActions.Dismissed`` is funnelled by the editor
        through its own ``action_dismiss``, so by the time we see this message the source
        is always the editor itself (carried as ``event.control``)."""
        editor = event.control
        for fid, widget in self._mounted.items():
            if widget is editor:
                self._vm._remove_feed(fid)
                self.query_one("#chat-input", ChatInput).focus()
                return

    # This view isn't focusable itself, so ``vm.request_focus()`` lands here — we route it to the
    # message-area scroll container. Keystrokes bubble back up to this view, so the commit-mode
    # bindings still fire.
    def focus(self, scroll_visible: bool = True) -> "ConversationArea":
        self.query_one("#message-area", VerticalScroll).focus(scroll_visible=scroll_visible)
        return self
