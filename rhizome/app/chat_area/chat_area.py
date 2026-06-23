"""ChatAreaModel — the conversation's view-model: user actions in, feed items + agent payloads out.

This is the final API the view drives. It owns the conversation **cursor** (the checked-out path
through the graph — business state, unlike widget carets: rebuilding the view from this VM must
restore the checkout) and composes the layers below:

- ``ConversationGraph`` carries the durable substance — feeds, names, interrupt slots, navigation
  memory, and the agent threads underneath. The VM subscribes to its model-level events and re-emits
  view-facing callbacks, so views only ever subscribe here.
- ``ChatAreaStreamRouter`` is constructed per ``submit`` and referenced by nobody afterwards: all
  run-lifecycle handling arrives through its own stream-context callbacks.

Policy that is deliberately *this* layer's and not the graph's: the two-children branch shape
(continuation + new branch on the first fork), the walk-up hoist from an empty leaf, consecutive
system-message dedup, and which roles forward to the agent as payloads.

Mode and verbosity travel as ``StateUpdatePayload``s into per-branch agent state — branches inherit
them through checkpoint copy and diverge after. Eager sends reach the *current* run's next model
call; idle sends wait in the backlog for the next run.

Feed-entry and interrupt VM classes are bridge-imported from ``chat_pane`` for now — they're already
view-models and move here wholesale when the old pane retires.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from rhizome.agent.engine import MessagePayload
from rhizome.agent.runtime import AgentRuntime
from rhizome.app.chat_pane.chat_input import ChatInputModel
from rhizome.app.chat_pane.command_palette import CommandPaletteModel
from rhizome.app.chat_pane.interrupts.base import CANCELLED
from rhizome.app.chat_pane.messages.shell import ShellCommandModel
from rhizome.app.chat_pane.messages.static import ChatMessageModel
from rhizome.app.chat_pane.welcome_message import WelcomeMessageModel
from rhizome.app.browser.browser import BrowserModel
from rhizome.app.commands import CommandError, CommandRegistry, CommandRegistryService, DefaultParser, Flag, RAW
from rhizome.app.model import ViewModelBase
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.db import SessionFactoryService
from rhizome.resources_new import ResourceContextStore, ResourceIndexStore
from rhizome.tui.types import Mode, Role

from .branch import BranchPointModel
from .conversation_graph import ConversationGraph, ConversationItem, ConversationNode, Cursor
from .demo_commands import register_demo_commands
from .status import StatusBarModel
from .stream_router import ChatAreaStreamRouter


class ChatAreaModel(ViewModelBase):

    class Callbacks(ViewModelBase.Callbacks):
        OnCursorMoved      = "OnCursorMoved"
        OnFeedAppended     = "OnFeedAppended"
        OnFeedRemoved      = "OnFeedRemoved"
        OnFeedCleared      = "OnFeedCleared"
        OnNodeRenamed      = "OnNodeRenamed"
        OnBusyChanged      = "OnBusyChanged"
        OnInterruptChanged = "OnInterruptChanged"

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        agent_key: str = "root",
        resource_context: ResourceContextStore | None = None,
        resource_index: ResourceIndexStore | None = None,
        local_resources_factory: Callable[[], ResourceContextStore] | None = None,
        command_registry: CommandRegistryService | None = None,
        options: OptionService | None = None,
        session_factory: SessionFactoryService | None = None,
        show_welcome: bool = False,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnCursorMoved:      Cursor,                                # the new cursor
            self.Callbacks.OnFeedAppended:     (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedRemoved:      (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedCleared:      ConversationNode,
            self.Callbacks.OnNodeRenamed:      ConversationNode,
            self.Callbacks.OnBusyChanged:      (ConversationNode, bool),              # see _notify_stream_complete
            self.Callbacks.OnInterruptChanged: ConversationNode,
        })

        self.runtime = runtime

        # Conversation-global references the commands + status bar read at invocation time: the option
        # service (model name + /options target) and the DB session factory (/browse + proposal demos).
        # Both optional — a standalone area runs without them, the dependent commands degrading to a hint.
        self._options = options
        self._session_factory = session_factory
        self._debug = debug

        # Worker scheduler, late-bound: the graph captures ``self._schedule`` at construction, and
        # the view swaps the underlying callable for Textual's ``run_worker`` at mount.
        self._scheduler: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.create_task

        self.conversation_graph: ConversationGraph = ConversationGraph(
            runtime,
            self._schedule,
            agent_key=agent_key,
            resource_context=resource_context,
            resource_index=resource_index,
            local_resources_factory=local_resources_factory,
        )
        self.conversation_graph.make_root()   # opens the topology — mints the root node after wiring
        self.conversation_graph.rename(self.conversation_graph.root, "main")
        self._cursor: Cursor = self.conversation_graph.root_cursor()

        # Model-level graph events re-emit as this VM's view-facing groups (views subscribe to their
        # VM, never to the graph directly). Bound-method subscribers are weakly held by the graph;
        # this VM outlives the graph it owns, so the subscriptions are safe.
        graph = self.conversation_graph
        graph.subscribe(graph.Callbacks.OnFeedAppended, self._on_graph_feed_appended)
        graph.subscribe(graph.Callbacks.OnFeedRemoved, self._on_graph_feed_removed)
        graph.subscribe(graph.Callbacks.OnFeedCleared, self._on_graph_feed_cleared)
        graph.subscribe(graph.Callbacks.OnNodeRenamed, self._on_graph_node_renamed)
        graph.subscribe(graph.Callbacks.OnNodeBusyChanged, self._on_graph_busy_changed)

        # Command registry + input + palette. The conversation registers its commands on the registry the
        # workspace injects (parented to the app-global scope), or on a bare fallback when constructed
        # standalone. The palette reads the registry's rows lazily; the input + palette are
        # conversation-agnostic VMs bridge-imported from chat_pane. This layer subscribes to the input's
        # OnSubmitted and routes (chat / shell / slash).
        self._commands: CommandRegistryService = command_registry if command_registry is not None else CommandRegistry()
        self._register_commands()
        self.command_palette = CommandPaletteModel(self._commands)
        self.chat_input = ChatInputModel(self.command_palette)
        self.chat_input.subscribe(self.chat_input.Callbacks.OnSubmitted, self._on_input_submitted)

        # Status bar — a fixed chat-area element (not a swappable panel). A projection of the
        # checked-out node's live mode/verbosity (its AppContextStore) plus the model name (from
        # options). The per-branch sources re-point in ``set_cursor`` so the bar tracks the visible branch.
        self.status_bar = StatusBarModel(self._options, self._cursor.node.app_state)

        # Seed the root feed with the welcome banner when the composition root opts in (the workspace
        # does; a standalone area stays empty). The greeting name comes from options when present.
        if show_welcome:
            user_name = self._options.get(Options.UserName) if self._options is not None else None
            self.append_item(WelcomeMessageModel(user_name=user_name or None))

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self._scheduler(coro)

    def set_worker_scheduler(self, scheduler: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
        """Swap the worker scheduler (the view injects Textual's ``run_worker`` on mount)."""
        self._scheduler = scheduler

    # ------------------------------------------------------------------
    # Graph event pass-through
    # ------------------------------------------------------------------

    def _on_graph_feed_appended(self, node: ConversationNode, item: ConversationItem) -> None:
        self.emit(self.Callbacks.OnFeedAppended, node, item)

    def _on_graph_feed_removed(self, node: ConversationNode, item: ConversationItem) -> None:
        self.emit(self.Callbacks.OnFeedRemoved, node, item)

    def _on_graph_feed_cleared(self, node: ConversationNode) -> None:
        self.emit(self.Callbacks.OnFeedCleared, node)

    def _on_graph_node_renamed(self, node: ConversationNode) -> None:
        self.emit(self.Callbacks.OnNodeRenamed, node)

    def _on_graph_busy_changed(self, node: ConversationNode, busy: bool) -> None:
        # Relayed from the agent layer's worker pinpoints, so the payload always agrees with
        # ``node.busy`` — the idle edge fires only after the run's teardown has settled.
        self.emit(self.Callbacks.OnBusyChanged, node, busy)

    # ------------------------------------------------------------------
    # Cursor & navigation
    # ------------------------------------------------------------------

    @property
    def cursor(self) -> Cursor:
        return self._cursor

    def _resolve(self, cursor: Cursor | ConversationNode | int | None) -> Cursor:
        """The target for an operation: the current checkout when ``cursor`` is None."""
        if cursor is None:
            return self._cursor
        return self.conversation_graph.cursor(cursor)

    def set_cursor(self, cursor: Cursor | ConversationNode | int) -> None:
        """Check out a path. Records the visit, pushes selected-child state to the branch
        indicators along it, and emits ``OnCursorMoved`` (the view re-derives the visible feed)."""
        path = self.conversation_graph.cursor(cursor)
        if path == self._cursor:
            return
        self._cursor = path
        self.conversation_graph.record_visit(path)
        self._sync_branch_indicators()
        self._sync_status_bar()
        self._sync_chat_input()
        self.emit(self.Callbacks.OnCursorMoved, path)

    def descend(self, child: ConversationNode | int) -> None:
        self._navigate(lambda: self.conversation_graph.descend(self._cursor, child))

    def ascend(self, *, to: ConversationNode | int | None = None) -> None:
        self._navigate(lambda: self.conversation_graph.ascend(self._cursor, to=to))

    def swap_sibling(self, direction: int, *, at: ConversationNode | int | None = None) -> None:
        self._navigate(lambda: self.conversation_graph.swap_sibling(self._cursor, direction, at=at))

    def _navigate(self, op: Callable[[], Cursor]) -> None:
        """Run a graph navigation op against the current cursor; boundary conditions (no sibling
        that way, already at the root) surface as hints, not exceptions — keystrokes get spammed."""
        try:
            path = op()
        except (KeyError, ValueError) as exc:
            self.hint(str(exc))
            return
        self.set_cursor(path)

    def _sync_branch_indicators(self) -> None:
        """Push cursor-derived selection to every branch indicator on the cursor path: the child the
        path descends into, or ``None`` when the indicator's node is the leaf. The setter is
        equality-guarded, so untouched indicators stay quiet. Off-path indicators keep their last
        selection until revisited — they aren't visible anyway."""
        nodes = self._cursor.nodes()
        for i, node in enumerate(nodes):
            selected = nodes[i + 1] if i + 1 < len(nodes) else None
            for item in node.feed:
                if isinstance(item.entry, BranchPointModel):
                    item.entry.set_selected_child(selected)

    def _sync_status_bar(self) -> None:
        """Point the status bar at the current leaf: its live settings store (mode/verbosity) and that
        branch's cached usage report, so both track the checked-out branch. The report is read from the
        node's cache (the stream router keeps it current), so this stays synchronous — no state re-fetch.
        No-op for a node without an app_state (none arise in practice)."""
        node = self._cursor.node
        if node.app_state is not None:
            self.status_bar.set_app_state(node.app_state)
        self.status_bar.set_usage_report(node.usage_report)

    def _sync_chat_input(self) -> None:
        """Enable/disable the chat input area after a cursor change, depending on whether a pending interrupt
        is active in the current branch."""
        node = self._cursor.node
        if node.pending_interrupt is None:
            self.chat_input.set_state(ChatInputModel.State.CHAT)
        else:
            self.chat_input.set_state(ChatInputModel.State.DISABLED_PENDING_INTERRUPT)

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def append_item(self, entry: Any, *, cursor: Cursor | None = None) -> ConversationItem:
        """Append an arbitrary feed entry to the target branch (current cursor by default)."""
        return self.conversation_graph.append(self._resolve(cursor), entry)

    def remove_item(self, item: ConversationItem | int, *, cursor: Cursor | None = None) -> ConversationItem | None:
        """Remove a feed item (or item id) from the target branch. Returns it, or None if absent."""
        item_id = item.id if isinstance(item, ConversationItem) else item
        return self.conversation_graph.remove(self._resolve(cursor), item_id)

    def append_message(
        self,
        content: str,
        role: Role = Role.SYSTEM,
        *,
        cursor: Cursor | None = None,
        to_agent: bool = True,
        eager: bool = False,
    ) -> ConversationItem | None:
        """Append a chat message to the feed and (optionally) forward it to the branch's agent.

        Dedup rule: a SYSTEM message identical to the tail of the target's *visible* feed is
        suppressed (returns None) — so a system message arriving in a freshly descended branch
        dedupes against an identical tail-of-parent message.

        Forwarding: USER and SYSTEM roles become ``MessagePayload``s on the branch's session (ERROR
        and other roles are UI-side noise the agent shouldn't react to). ``eager=True`` posts into
        the *current* run mid-stream — the session's eager queue — instead of the next run's backlog.
        """
        target = self._resolve(cursor)

        visible = self.conversation_graph.visible_feed(target)
        tail = visible[-1].entry if visible else None
        if (
            role == Role.SYSTEM
            and isinstance(tail, ChatMessageModel)
            and tail.role == Role.SYSTEM
            and tail.content == content
        ):
            # TODO: ping the existing entry (visual nudge) instead of silently swallowing.
            return None

        item = self.conversation_graph.append(target, ChatMessageModel(role=role, content=content))

        if to_agent and role in (Role.USER, Role.SYSTEM):
            payload_role = MessagePayload.Role.USER if role == Role.USER else MessagePayload.Role.SYSTEM
            self.conversation_graph.send(target, MessagePayload(data=content, role=payload_role), eager=eager)

        return item

    def clear(self) -> None:
        """Clear the entire conversation slate (every branch). TODO: semantics under the graph are
        unsettled — see also the eventual ``clear_branch`` (delete one branch)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Per-branch agent state (mode / verbosity)
    # ------------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        """The checked-out branch's active mode — read off its ``AppContextStore`` (the SSOT)."""
        return Mode(self._cursor.node.app_state.mode)

    @property
    def verbosity(self) -> str:
        """The checked-out branch's answer verbosity — read off its ``AppContextStore`` (the SSOT)."""
        return self._cursor.node.app_state.verbosity

    def set_mode(self, mode: Mode, *, cursor: Cursor | None = None, silent: bool = False) -> None:
        """Switch the branch's mode by writing its live ``AppContextStore`` — the single source of truth
        both the user (here) and the agent (the ``set_mode`` tool) write through.

        The store is always live, so the change needs no eager send: the prompt engine reads it at the
        next compile (mid-run or next run), commits it into agent state, and narrates it agent-side
        (guides/headers). The feed message here is UI-only.
        """
        target = self._resolve(cursor)
        self.conversation_graph.node(target).app_state.set_mode(mode.value)

        if not silent:
            text = "Returned to idle mode." if mode == Mode.IDLE else f"Entered {mode.value} mode."
            self.append_message(text, Role.SYSTEM, cursor=target, to_agent=False)

    def set_verbosity(self, verbosity: str, *, cursor: Cursor | None = None) -> None:
        """Switch the branch's answer verbosity by writing its live ``AppContextStore`` — the per-branch
        SSOT the status bar projects, mirroring ``set_mode``. No prompt-engine consumer reads it yet
        (see ``AppContextStore``), so this is the view-facing setting only for now."""
        target = self._resolve(cursor)
        self.conversation_graph.node(target).app_state.set_verbosity(verbosity)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def agent_busy(self, cursor: Cursor | None = None) -> bool:
        """A run is in flight on the target branch. Cancellation is cooperative — this stays True
        until the unwind completes; ``OnBusyChanged`` fires at the exact flip."""
        return self.conversation_graph.node(self._resolve(cursor)).busy

    def pending_interrupt(self, cursor: Cursor | None = None) -> Any | None:
        """The interrupt VM blocking the target branch on user input, if any."""
        return self.conversation_graph.node(self._resolve(cursor)).pending_interrupt

    def submit(self, *, cursor: Cursor | None = None) -> None:
        """Start a run on the target branch: everything in the session's backlog (queued
        ``append_message`` payloads, mode changes, ...) is ingested at the run's first model call.

        Soft-fails with a hint when the branch is frozen or already running — both are states the
        user can reach with ordinary keystrokes, not programming errors.
        """
        path = self._resolve(cursor)
        node = self.conversation_graph.node(path)
        if node.frozen:
            self.hint("this branch is frozen — descend into one of its children")
            return
        if node.busy:
            self.hint("the agent is already responding on this branch")
            return

        router = ChatAreaStreamRouter(self, path)  # mounts the thinking indicator
        self.conversation_graph.stream(path, router)  # fires OnNodeBusyChanged(node, True)

    def cancel(self, *, cursor: Cursor | None = None) -> None:
        """Cancel the target branch's in-flight run. Cooperative: teardown (history repair, the
        "(user cancelled)" message, indicator removal, the busy-flip event) runs when the
        cancellation unwinds through the router's callbacks."""
        self.conversation_graph.cancel(self._resolve(cursor))

    # ------------------------------------------------------------------
    # Interrupts
    # ------------------------------------------------------------------

    async def present_interrupt(self, vm: Any, *, cursor: Cursor | None = None) -> Any:
        """Append an interrupt VM to the target feed and await its resolution.

        The VM stays in the feed afterwards as an inert record. ``pending_interrupt`` on the node is
        the "blocked on user input" flag for the duration; ``OnInterruptChanged`` fires on both
        edges so views derive input lockout from it.

        Cancellation discipline: dismissal (``vm.cancel()``) resolves the future with the
        ``CANCELLED`` sentinel — translated to None here, and the run continues. A real
        ``CancelledError`` therefore always means the *task* was cancelled (the user killed the
        run); it propagates untouched so the session's cancel path runs.
        """
        target = self._resolve(cursor)
        node = self.conversation_graph.node(target)

        node.pending_interrupt = vm
        self.conversation_graph.append(target, vm)

        # Disable chat input if the branch where the interrupt was appended is the current branch.
        # An interrupt in a non-current branch will disable the chat input (for that branch) on cursor
        # motions into it. 
        if target.node == self._cursor.node:
            self.chat_input.set_state(ChatInputModel.State.DISABLED_PENDING_INTERRUPT)

        self.emit(self.Callbacks.OnInterruptChanged, node)

        try:
            result = await vm.future()
            return None if result is CANCELLED else result
        finally:
            node.pending_interrupt = None
            # Re-enable chat input area (again, if we're current branch)
            if target.node == self._cursor.node:
                self.chat_input.set_state(ChatInputModel.State.CHAT)

            self.emit(self.Callbacks.OnInterruptChanged, node)

    # ------------------------------------------------------------------
    # Branching
    # ------------------------------------------------------------------

    async def branch(
        self,
        name: str | None = None,
        prompt: str | None = None,
        *,
        cursor: Cursor | None = None,
    ) -> Cursor | None:
        """Fork the conversation at the target and check out the new branch.

        This is where "branch = two children" lives, deliberately above the graph's one-child
        primitive: the first fork at a live leaf creates a *continuation* (leftmost, inheriting the
        name — the original line stays writable) plus the new branch; later forks at the same node
        just add siblings. The branch indicator must land in the parent's feed before the first
        ``graph.branch`` freezes it — the sealed-history rule enforces the ordering.

        Walk-up hoist: branching from an empty live leaf hoists to its parent — the natural reading
        of "/branch right after descending into a fresh branch" is "give me another sibling", not a
        branch buried inside an empty node.

        ``prompt`` is appended as the first USER message on the new branch and submitted.
        Soft-fails (hint, returns None) when the target is mid-run.
        """
        graph = self.conversation_graph
        path = self._resolve(cursor)
        node = path.node

        if path.parent is not None and not node.frozen and not node.feed:
            path = path.parent
            node = path.node

        if node.busy:
            self.hint("cannot branch while the agent is responding")
            return None

        if not node.frozen:
            # First fork here: indicator, then the continuation child (leftmost, keeps the name).
            graph.append(path, BranchPointModel(self, node))
            continuation = await graph.branch(path)
            graph.rename(continuation.node, node.name)

        new = await graph.branch(path)
        if name:
            graph.rename(new.node, name)

        self.set_cursor(new)
        if prompt:
            self.append_message(prompt, Role.USER)
            self.submit()
        return new

    def set_branch_name(self, name: str, *, cursor: Cursor | None = None) -> None:
        """Rename the target branch (current cursor's leaf by default). An empty name clears it. The
        graph emits ``OnNodeRenamed``; the indicator on the parent's feed re-derives the label from it."""
        target = self._resolve(cursor)
        self.conversation_graph.rename(target, name.strip() or None)

    # ------------------------------------------------------------------
    # Input dispatch
    # ------------------------------------------------------------------

    def _on_input_submitted(self, text: str) -> None:
        """Route the chat input's submitted text to a shell command, a slash command, or an agent turn.

        ``text`` arrives stripped. The input VM holds the buffer until ``accept_submission`` clears it, so
        a gated/rejected submission stays editable for retry. Plain chat is gated on the current leaf the
        way ``submit`` is (busy / frozen → hint); shell and slash commands are side-channel and pass
        through. Slash commands go straight to the registry — its ``RAW`` parser keeps a command's free-text
        remainder intact (so ``/branch Can't Stop`` just works), with no special-casing here.
        """
        stripped = text.lstrip()

        if stripped.startswith("!"):
            command = stripped[1:].strip()
            if command:
                self.chat_input.accept_submission(text)
                self.submit_shell_command(command)
            return

        if stripped.startswith("/"):
            self.chat_input.accept_submission(text)
            self.submit_slash_command(stripped)
            return

        # Plain chat: pre-check the leaf the way ``submit`` does, but before consuming the buffer or
        # appending — ``append`` refuses a frozen node outright, and a busy node would queue the message
        # into the next run rather than block (the parity behaviour we keep for now). A blocked message
        # stays in the buffer for retry.
        node = self._cursor.node
        if node.busy:
            self.hint("the agent is already responding on this branch")
            return
        if node.frozen:
            self.hint("this branch is frozen — descend into one of its children")
            return

        self.chat_input.accept_submission(text)
        self.append_message(text, Role.USER)
        self.submit()

    def submit_shell_command(self, command: str) -> None:
        """Run a ``!`` shell command as a feed-resident widget: append a ``ShellCommandModel`` and kick
        off its ``execute`` coroutine on the worker scheduler. Side-channel to the conversation — not
        gated by ``agent_busy`` and never forwarded to the agent."""
        vm = ShellCommandModel(command)
        self.append_item(vm)
        self._schedule(vm.execute())

    def submit_slash_command(self, line: str) -> None:
        """Dispatch a ``/`` command through the registry (async, so it schedules). Help/echo results and
        errors land in the feed; nothing is forwarded to the agent."""
        self._schedule(self._execute_command(line))

    async def _execute_command(self, line: str) -> None:
        try:
            result = await self._commands.execute(line)
        except CommandError as exc:
            self.append_item(ChatMessageModel(role=Role.ERROR, content=str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 — surface unexpected handler errors as ERROR messages
            self.append_item(ChatMessageModel(role=Role.ERROR, content=f"Command error: {exc}"))
            return
        if result is not None:
            self.append_item(ChatMessageModel(role=Role.SYSTEM, content=str(result), rich=True))

    # ------------------------------------------------------------------
    # Command registry
    # ------------------------------------------------------------------

    @property
    def commands(self) -> CommandRegistryService:
        """The conversation's command registry (parented to the workspace/global scope when injected)."""
        return self._commands

    @property
    def session_factory(self) -> SessionFactoryService | None:
        """The DB session factory, if one was injected — used by /browse and the proposal demo commands."""
        return self._session_factory

    def _register_commands(self) -> None:
        """Register the conversation-scoped slash commands. Tab / app commands (/quit, /new, ...) live in
        the global registry this scope inherits from, /help is built into the registry core, and
        /resources is workspace-scoped. /commit lands with commit mode; /clear's real semantics are still
        open (see ``_cmd_clear``)."""
        reg = self._commands
        reg.register("idle", lambda: self.set_mode(Mode.IDLE), help="Switch to idle mode.")
        reg.register("learn", lambda: self.set_mode(Mode.LEARN), help="Switch to learn mode.")
        reg.register("review", lambda: self.set_mode(Mode.REVIEW), help="Switch to review mode.")
        reg.register("branch", self._cmd_branch, parser=RAW,
                     help="Fork a new branch; optionally provide a prompt to send.")
        reg.register("rename-branch", self._cmd_rename_branch, parser=RAW, help="Rename the current branch.")
        reg.register("browse", self._cmd_browse, help="Open the data browser inline in the feed.")
        reg.register("options", self._cmd_options, help="Open the options editor inline in the feed.",
                     parser=DefaultParser(flags=[Flag(
                         "global", short="-g",
                         help="Edit global (Root) options instead of this conversation's session options.")]))
        reg.register("clear", self._cmd_clear, help="Clear the message feed.")
        reg.register("echo", lambda text: text, parser=RAW, help="Echo arguments back as a system message.")

        # Demo / exercise commands (/test-*) — registered only under the app's --debug flag (off otherwise,
        # including for a standalone area). See the module docstring.
        if self._debug:
            register_demo_commands(self)

    async def _cmd_branch(self, rest: str) -> None:
        await self.branch(prompt=rest or None)

    def _cmd_rename_branch(self, name: str) -> None:
        self.set_branch_name(name)

    def _find_visible_entry(self, predicate: Callable[[Any], bool]) -> Any | None:
        """First feed entry along the current cursor path (root→leaf) satisfying ``predicate``.

        The path back to root is exactly the visible feed, so this covers an entry mounted on any
        ancestor branch, not just the checked-out leaf. Commands for singleton widgets use it to
        focus an already-open instance instead of stacking a duplicate.
        """
        for item in self.conversation_graph.visible_feed(self._cursor):
            if predicate(item.entry):
                return item.entry
        return None

    def _cmd_browse(self) -> None:
        if self._session_factory is None:
            self.hint("/browse is unavailable: no database session in this scope")
            return
        existing = self._find_visible_entry(lambda e: isinstance(e, BrowserModel))
        if existing is not None:
            existing.request_focus()
            return
        self.append_item(BrowserModel(self._session_factory))

    def _cmd_options(self, *, global_: bool) -> None:
        if self._options is None:
            self.hint("/options is unavailable: no options service in this scope")
            return
        # ``--global`` reaches the Root node; the default targets this conversation's own session options.
        target = self._options.at_scope(OptionScope.Root) if global_ else self._options
        # Reuse an open editor for *this same scope* — root and session are distinct target objects, so
        # an identity match keeps the global and local editors from colliding.
        existing = self._find_visible_entry(
            lambda e: isinstance(e, OptionsEditorModel) and e.target is target
        )
        if existing is not None:
            existing.request_focus()
            return
        self.append_item(OptionsEditorModel(target))

    def _cmd_clear(self) -> None:
        # TODO(graph): /clear semantics under the conversation graph are unsettled — which branch's feed
        # counts, whether to cancel in-flight turns, and how to treat work on other branches. No-op + hint.
        self.hint("/clear isn't wired up under the conversation graph yet")

    # ------------------------------------------------------------------
    # Commit mode (stubs)
    # ------------------------------------------------------------------

    def enter_commit_mode(self) -> None:
        """TODO: commit mode is being reworked — selection/cursor mechanics move view-side."""
        raise NotImplementedError
