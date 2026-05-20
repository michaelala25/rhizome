"""ChatPaneViewModel — steps 1–3 of the chat-pane MVVM rewrite.

Steps 1+2 cover the feed + commands; step 3 adds an ``AgentSession`` instance, held but unused. No
worker, no streaming, no harness yet — this is just the bootstrap seam.

Out of scope: starting/cancelling agent runs, sub-VMs in the feed, status-bar projection, shell ``!``
commands, agent gating of commands, the agent-busy half of the mode-transition matrix.
"""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

import rich_click as click
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rhizome.agent.session import AgentSession, get_agent_kwargs
from rhizome.db import Topic
from rhizome.db.operations import get_topic
from rhizome.resources.manager import ResourceManager
from rhizome.tui.commands import CommandRegistry
from rhizome.tui.options import Options
from rhizome.tui.types import ChatMessageData, Mode, Role

from ..view_model_base import ViewModelBase
from .agent_message import AgentMessageViewModel
from .agent_stream_router import AgentStreamRouter
from .chat_input import ChatInputViewModel
from .command_palette import CommandPaletteViewModel
from .interrupt import InterruptViewModelBase, TestInterruptViewModel
from .shell_command import ShellCommandViewModel
from .status_bar import StatusBarViewModel
from .thinking_indicator import ThinkingIndicatorViewModel
from .tool_message import ToolMessageViewModel


FeedEntry = (
    ChatMessageData
    | AgentMessageViewModel
    | ToolMessageViewModel
    | ThinkingIndicatorViewModel
    | InterruptViewModelBase
    | ShellCommandViewModel
)


@dataclass
class FeedItem:
    """Wraps a feed entry with a stable, monotonically-assigned id.

    The id is the canonical handle for feed mutations (remove, future replace/ping). Position is not
    identity: a feed item's index can shift as later code adds out-of-band operations, so consumers
    that need to address a specific item must hold its id rather than its index.
    """
    id: int
    entry: FeedEntry


_DEFAULT_HINT = "Type a message or /command ..."
_INTERRUPT_HINT = "Resolve the prompt above to continue..."


class ChatPaneViewModel(ViewModelBase):

    # Slash commands that must wait for the agent to be idle. Anything not in this set is allowed to
    # dispatch mid-stream (mode toggles, echo, test-* helpers, etc.). Shell `!` commands and free-text
    # chat are gated separately in ``_on_input_submitted``.
    _AGENT_GATED_COMMANDS: frozenset[str] = frozenset({"commit"})

    class State(Enum):
        """Coarse VM state. Most of the public API asserts ``state == CONVERSATION``; the COMMIT
        branch is entered via ``enter_commit_mode`` and exited via ``exit_commit_mode`` or
        ``submit_commit_payload``.
        """
        CONVERSATION = "conversation"
        COMMIT = "commit"

    class Callbacks(Enum):
        FEED_APPEND = "feed_append"
        FEED_REMOVE = "feed_remove"
        FEED_CLEAR = "feed_clear"
        TAB_RENAME = "tab_rename"
        NOTIFY = "notify"

    class NotifyAction(Enum):
        """Actions the VM asks the parent view to perform. The VM specifies the action; the view
        owns the concrete user-facing presentation (message text, severity, timeout, routing to a
        sibling pane, etc.). Use this for anything the chat-pane VM can't accomplish directly —
        eventually things like "open logs pane" once an app-level VM exists.
        """
        HINT_HIGHER_VERBOSITY = "hint_higher_verbosity"
        AGENT_BUSY = "agent_busy"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        super().__init__()

        self._feed_append = self._make_group(ChatPaneViewModel.Callbacks.FEED_APPEND)
        self._feed_remove = self._make_group(ChatPaneViewModel.Callbacks.FEED_REMOVE)
        self._feed_clear = self._make_group(ChatPaneViewModel.Callbacks.FEED_CLEAR)
        self._tab_rename = self._make_group(ChatPaneViewModel.Callbacks.TAB_RENAME)
        self._notify = self._make_group(ChatPaneViewModel.Callbacks.NOTIFY)

        self.feed: list[FeedItem] = []
        self._next_feed_id: int = 0

        self.state: ChatPaneViewModel.State = ChatPaneViewModel.State.CONVERSATION

        # Commit-mode working set, valid only while ``state == COMMIT``. ``_commit_selectable`` is
        # the snapshot of learn-mode AgentMessageVMs in feed order at enter time; ``_commit_cursor``
        # is the index of the highlighted entry. Reset by ``exit_commit_mode`` /
        # ``submit_commit_payload``.
        self._commit_selectable: list[AgentMessageViewModel] = []
        self._commit_cursor: int = 0

        self.session_mode: Mode = Mode.IDLE

        # Active topic + path from the topic tree root. Mutated via set_topic / clear_topic; surfaced
        # by the view in the status bar.
        self.active_topic: Topic | None = None
        self.topic_path: list[str] = []

        self.command_palette = CommandPaletteViewModel()
        self._command_registry = CommandRegistry()
        self._register_commands()
        self.command_palette.set_commands(self._registry_rows())

        # Input sub-VM owns buffer/enabled/hint/history + holds the shared palette so the input view
        # never reaches into the pane to filter, navigate, or decide tab-completion vs submit. The pane
        # subscribes to ``submitted`` to dispatch chat-vs-slash + agent-busy gating.
        self.chat_input = ChatInputViewModel(self.command_palette, default_hint=_DEFAULT_HINT)
        self.chat_input.subscribe(self.chat_input.submitted, self._on_input_submitted)

        # Status-bar sub-VM. Projection of mode / topic_path (from this VM), token_usage + model_name
        # (from the agent session), and verbosity (from app.options). Pane mutates it through setters;
        # the view subscribes to its own dirty so token updates don't repaint the rest of the pane.
        self.status_bar = StatusBarViewModel()
        self._options: Options | None = None

        # Agent plumbing — instantiated on bootstrap (after the view has access to app.options). Held
        # but unused at step 3.
        self._session_factory = session_factory
        self.resource_manager: ResourceManager | None = (
            ResourceManager(session_factory=session_factory) if session_factory else None
        )
        self.agent_session: AgentSession | None = None

        # Agent stream router (transient): constructed at the start of each turn by
        # ``_run_agent_turn`` (or a synthetic-test driver) and discarded in the same finally block
        # that closes it. The pane holds this slot during the turn so the interrupt path can reach
        # the in-flight router; ``None`` between turns. ``agent_busy`` (worker task aliveness) is
        # the single source of truth for "is the agent running".
        self._current_router: AgentStreamRouter | None = None

        # The interrupt VM currently awaiting user input, if any. Tracked so external callers
        # (eventually: a cancel-during-agent-run path) can resolve it from outside the awaiting
        # coroutine.
        self._pending_interrupt: InterruptViewModelBase | None = None

        # A non-None worker means an agent turn is in flight. Cleared by the worker's own finally
        # block, so cancellation is effectively synchronous from the caller's perspective.
        self._agent_task: object | None = None

        # Scheduler hook: the View overrides this on mount to use Textual's ``run_worker`` (lifecycle
        # binds to the widget, errors surface via the app, DevTools sees the worker). Default plain
        # ``create_task`` keeps headless/test usage working.
        self._schedule_worker: Callable[[Coroutine[Any, Any, Any]], object] = asyncio.create_task

    # ------------------------------------------------------------------
    # Callback group accessors
    # ------------------------------------------------------------------

    @property
    def feed_append(self):
        return self._feed_append

    @property
    def feed_remove(self):
        return self._feed_remove

    @property
    def feed_clear(self):
        return self._feed_clear

    @property
    def tab_rename(self):
        return self._tab_rename

    @property
    def notify(self):
        return self._notify

    @property
    def command_registry(self) -> CommandRegistry:
        return self._command_registry

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def set_worker_scheduler(self, scheduler: Callable[[Coroutine[Any, Any, Any]], object]) -> None:
        """Inject a scheduler for spawning long-running coroutines (agent turn, async command
        handlers). Called by the View on mount with Textual's ``run_worker``; tests / headless callers
        can leave the default ``asyncio.create_task``.
        """
        self._schedule_worker = scheduler

    def bootstrap_agent_session(self, app_options: Options, *, debug: bool = False) -> None:
        """Construct ``self.agent_session`` from app options. Idempotent: a second call is a no-op.
        Caller is the view's ``on_mount`` because the provider/model values live on Textual's
        ``app.options``.
        """
        if self.agent_session is not None:
            return
        if self._session_factory is None:
            return

        self._options = app_options

        provider = app_options.get(Options.Agent.Provider)
        model_name = app_options.get(Options.Agent.Model)
        agent_kwargs = get_agent_kwargs(app_options)

        self.agent_session = AgentSession(
            self._session_factory,
            chat_pane=self,
            resource_manager=self.resource_manager,
            provider=provider,
            model_name=model_name,
            agent_kwargs=agent_kwargs,
            on_token_usage_changed=self._on_token_usage_changed,
            debug=debug,
        )

        # Seed status-bar fields that come from the agent session / options.
        self.status_bar.set_model_name(self.agent_session._model_name or "")
        self.status_bar.set_verbosity(app_options.get(Options.Agent.AnswerVerbosity))
        app_options.subscribe(Options.Agent.AnswerVerbosity, self._on_verbosity_changed)

    def _on_token_usage_changed(self) -> None:
        if self.agent_session is not None:
            self.status_bar.set_token_usage(self.agent_session.token_usage)

    async def _on_verbosity_changed(self, _old, new) -> None:
        self.status_bar.set_verbosity(new)

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def _append_feed(self, entry: FeedEntry) -> FeedItem:
        """Wrap ``entry`` in a ``FeedItem`` with a fresh id, append it, and emit ``feed_append`` with
        the new id. Returns the wrapper so callers can hold the id for later mutations.
        """
        item = FeedItem(id=self._next_feed_id, entry=entry)
        self._next_feed_id += 1
        self.feed.append(item)
        self.emit(self.feed_append, item.id)
        return item

    def _remove_feed(self, item_id: int) -> None:
        """Remove the feed item with the given id, emitting ``feed_remove``. No-op if not found."""
        for i, item in enumerate(self.feed):
            if item.id == item_id:
                del self.feed[i]
                self.emit(self.feed_remove, item_id)
                return

    def append_message(self, msg: ChatMessageData, *, include_in_agent_context: bool = True) -> None:
        """Append a message to the feed, applying the consecutive-system dedup rule.

        If the previous feed entry is a system message with identical content, we suppress the append;
        otherwise the message is appended and ``feed_append`` fires with the new item id. Does **not**
        close any open agent turn — see "Feed ordering rules" above ``open_agent_turn``.

        When ``include_in_agent_context`` is True (the default), USER messages are pushed to the agent
        via ``add_human_message`` and SYSTEM messages via ``add_system_notification`` so they land in
        the conversation history on the next stream. Other roles (ERROR, etc.) are never forwarded —
        they're UI-side noise the agent shouldn't react to. Callers wanting feed-only mutation (test
        commands, UI echoes, legacy ``ui_only=True`` cases) pass ``include_in_agent_context=False``.
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        tail_entry = self.feed[-1].entry if self.feed else None
        if (
            msg.role == Role.SYSTEM
            and isinstance(tail_entry, ChatMessageData)
            and tail_entry.role == Role.SYSTEM
            and tail_entry.content == msg.content
        ):
            # TODO: ping the existing entry. Will likely flow through a per-entry dirty once
            # ChatMessageData (or a wrapping sub-VM) owns its own emit channel.
            return

        self._append_feed(msg)

        if include_in_agent_context and self.agent_session is not None:
            if msg.role == Role.USER:
                self.agent_session.add_human_message(msg.content)
            elif msg.role == Role.SYSTEM:
                self.agent_session.add_system_notification(msg.content)

    def clear_feed(self) -> None:
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        if not self.feed and self._current_router is None:
            return

        self._abandon_agent_turn()

        if self.feed:
            self.feed.clear()
            self.emit(self.feed_clear)

        # The pane state may have changed (agent_busy flipped to False if we abandoned a turn);
        # repaint so the input reflects that.
        self.emit(self.dirty)

    def _abandon_agent_turn(self) -> None:
        """Forcefully tear down the in-flight agent turn without posting user-facing artifacts."""
        if self._current_router is None:
            return

        self._current_router = None
        if self._agent_task is not None:
            task = self._agent_task
            self._agent_task = None
            # ``_agent_task`` is typed ``object`` to keep Textual's Worker out of this VM's
            # surface, but at runtime it's always cancellable (asyncio.Task or Textual Worker).
            task.cancel()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Feed ordering rules
    # ------------------------------------------------------------------
    #
    # The feed is append-only and addressed by ``FeedItem.id`` — **position is not identity**.
    # Agent-turn routing lives in ``AgentStreamRouter`` (see ``agent_stream_router.py``), one
    # instance per turn. ``_run_agent_turn`` constructs the router (which mounts the thinking
    # indicator), stores it on ``_current_router`` for the duration, and closes + discards it in
    # its finally block.
    # The router's stream callbacks route chunks/tool calls into chat/tool segments and repin the
    # indicator; ``present_interrupt`` reaches into ``_current_router`` to pause before awaiting
    # the user. ``agent_busy`` (worker task aliveness) is the single source of truth for "is the
    # agent running".


    # ------------------------------------------------------------------
    # Interrupts
    # ------------------------------------------------------------------

    async def present_interrupt(self, vm: InterruptViewModelBase) -> Any:
        """Append an interrupt VM to the feed and await its resolution.

        Closes any currently-open agent turn (peek-tail). Disables the chat input + swaps in a
        contextual hint for the duration; restores both on resolve/cancel. Returns the resolved value,
        or ``None`` if cancelled.

        The interrupt VM stays in the feed after resolution as an inert record — the View dims it but
        doesn't remove it, so the conversation history reflects what was chosen.
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION

        # TODO: It is up in the air whether or not we want to pause the current router to post _any_ interrupt
        # or let the router itself pause when posting it's own interrupt (through the on_interrupt handler).
        #
        # The behaviour if we pause here is as follows: while the agent is responding, if a separate interrupt
        # is posted to the feed, then the .pause() call will close the current agent message, then append the
        # interrupt to the feed, then the next agent response chunk will spawn a _new_ agent message _below_
        # the interrupt. In other words, calling .pause() here means exogenous interrupts "break agent messages
        # in half".

        # if self._current_router is not None:
        #     self._current_router.pause()

        self._pending_interrupt = vm
        self._append_feed(vm)

        prev_enabled = self.chat_input.enabled
        prev_hint = self.chat_input.hint
        self.chat_input.set_enabled(False)
        self.chat_input.set_hint(_INTERRUPT_HINT)

        try:
            return await vm.future()
        except asyncio.CancelledError:
            return None
        finally:
            self._pending_interrupt = None
            self.chat_input.set_enabled(prev_enabled)
            self.chat_input.set_hint(prev_hint)

    # ------------------------------------------------------------------
    # Agent run lifecycle
    # ------------------------------------------------------------------
    #
    # ``start_agent_run`` appends the user message, queues it on the AgentSession, and spawns
    # ``_run_agent_turn`` via the injected ``_schedule_worker``. ``_run_agent_turn`` drives
    # ``AgentSession.stream`` with VM-side callbacks that route into the open AgentMessage VM. Under
    # Textual, the View injects ``self.run_worker`` so the worker's lifecycle binds to the widget.

    @property
    def agent_busy(self) -> bool:
        """An agent turn is in flight whenever the worker task is alive."""
        return self._agent_task is not None

    def start_agent_run(self, user_text: str) -> None:
        """Append the user message and kick off an agent turn. No-op if a run is already in flight
        (real queueing comes with the feed-queue)."""
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        if self.agent_busy:
            return

        if self.agent_session is None:
            self.append_message(ChatMessageData(role=Role.ERROR, content="Agent session not bootstrapped."))
            return

        self.append_message(ChatMessageData(role=Role.USER, content=user_text))
        self._start_agent_turn()

    def _start_agent_turn(self) -> None:
        """Spawn ``_run_agent_turn`` on the worker scheduler. Used by ``start_agent_run`` (which
        prefixes a USER message) and by ``submit_commit_payload`` (which kicks off agent-only,
        driven by an injected commit payload + system notification, with no USER message).

        Caller preconditions: ``state == CONVERSATION``, ``agent_session`` non-None, not
        ``agent_busy``. No-op if already busy (defensive).
        """
        if self.agent_busy:
            return
        self._agent_task = self._schedule_worker(self._run_agent_turn())
        self.emit(self.dirty)


    async def _run_agent_turn(self) -> None:
        assert self.agent_session is not None

        router = AgentStreamRouter(self)
        self._current_router = router

        try:
            await self.agent_session.stream(
                mode=self.session_mode.value,
                on_message=router.on_message,
                on_update=router.on_update,
                on_interrupt=router.on_interrupt,
            )

        except asyncio.CancelledError:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="(user cancelled)"))
            raise

        except Exception as exc:  # noqa: BLE001 — surface stream errors as ERROR messages
            if self._current_router is router:
                self.append_message(ChatMessageData(role=Role.ERROR, content=f"Agent error: {exc}"))

        finally:
            if self._current_router is router:
                router.close()
                self._current_router = None
                self._agent_task = None
                self.emit(self.dirty)


    # ------------------------------------------------------------------
    # Session mode
    # ------------------------------------------------------------------

    async def cycle_mode(self) -> None:
        """Advance through IDLE → LEARN → REVIEW → IDLE. Silent — the binding's intent is a quick
        cycle, not a chat-visible mode change.
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        cycle = {Mode.IDLE: Mode.LEARN, Mode.LEARN: Mode.REVIEW, Mode.REVIEW: Mode.IDLE}
        await self.set_mode(cycle[self.session_mode], silent=True)

    async def cycle_verbosity(self) -> None:
        """Advance through ``Options.Agent.AnswerVerbosity.choices``. The existing options
        subscription updates the status bar VM."""
        if self._options is None:
            return
        choices = Options.Agent.AnswerVerbosity.choices
        current = self._options.get(Options.Agent.AnswerVerbosity)
        idx = choices.index(current) if current in choices else 0
        new_value = choices[(idx + 1) % len(choices)]
        await self._options.set(Options.Agent.AnswerVerbosity, new_value)
        await self._options.post_update()

    def _set_session_mode(self, mode: Mode) -> None:
        """Assign session_mode and forward to the status-bar VM in one place."""
        self.session_mode = mode
        self.status_bar.set_mode(mode.value)

    async def set_mode(
        self, mode: Mode, *, silent: bool = False, source: Literal["user", "agent"] = "user",
    ) -> None:
        """Set the session mode.

        Args:
            mode: target mode.
            silent: suppress the chat system message (shift+tab cycling, agent-initiated changes).
                Forced True when source=="agent".
            source: ``"user"`` (UI-initiated — queues a pending change on the agent middleware so
                graph state catches up on the next model call) or ``"agent"`` (tool-initiated — graph
                state is updated directly via ``Command``).
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        if source == "agent":
            assert self.agent_busy
            silent = True

        if self.session_mode == mode:
            if not silent:
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=f"Already in {mode.value} mode.")
                )
            return

        message = "Returned to idle mode." if mode == Mode.IDLE else f"Entered {mode.value} mode."

        if source == "agent":
            self._set_session_mode(mode)
            self.emit(self.dirty)
            # User-initiated mode set while the agent was running is superseded by the agent's tool
            # call.
            if self.agent_session is not None:
                await self.agent_session._mode_middleware.clear_pending_user_mode()
            return

        # source == "user"
        if self.agent_busy:
            self._set_session_mode(mode)
            if self.agent_session is not None:
                await self.agent_session.set_pending_user_mode(mode.value)
            if not silent:
                # Feed-only: set_pending_user_mode handles the agent side when the queue drains.
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=message, mode=mode),
                    include_in_agent_context=False,
                )
        else:
            self._set_session_mode(mode)
            if silent:
                if self.agent_session is not None:
                    self.agent_session.add_system_notification(message)
            else:
                self.append_message(ChatMessageData(role=Role.SYSTEM, content=message, mode=mode))

        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Topic / tab / verbosity (agent-tool-facing API)
    # ------------------------------------------------------------------

    def clear_topic(self) -> None:
        """Drop the active topic. View re-renders the status bar via dirty."""
        if self.active_topic is None and not self.topic_path:
            return
        self.active_topic = None
        self.topic_path = []
        self.status_bar.set_topic_path([])
        self.emit(self.dirty)

    async def set_topic(self, topic_id: int) -> bool:
        """Resolve ``topic_id`` and set it as the active topic. Walks parents to build ``topic_path``.
        Returns False if the topic does not exist (state is left unchanged).
        """
        if self._session_factory is None:
            return False

        async with self._session_factory() as session:
            topic = await get_topic(session, topic_id)
            if topic is None:
                return False
            path: list[str] = [topic.name]
            current = topic
            while current.parent_id is not None:
                current = await get_topic(session, current.parent_id)
                if current is None:
                    break
                path.append(current.name)
            path.reverse()

        self.active_topic = topic
        self.topic_path = path
        self.status_bar.set_topic_path(path)
        self.emit(self.dirty)
        return True

    async def set_tab_name(self, name: str) -> None:
        """Rename the active tab. The VM doesn't own tab state — emits through the ``tab_rename``
        callback group for the view (or a Tabs-level VM) to apply.
        """
        new_name = name.strip()
        if not new_name:
            return
        self.emit(self.tab_rename, new_name)

    def hint_higher_verbosity(self) -> None:
        """Hint to the user that a higher verbosity setting may help. The view decides how to
        present the cue.
        """
        self.emit(self.notify, ChatPaneViewModel.NotifyAction.HINT_HIGHER_VERBOSITY)

    # ------------------------------------------------------------------
    # Input dispatch
    # ------------------------------------------------------------------

    def _on_input_submitted(self, text: str) -> None:
        """Subscriber on ``chat_input.submitted``: route the submitted text to a command, a shell
        command, or an agent turn. Text is already buffer-cleared and history-pushed by the input VM
        by the time we see it.

        Agent-busy gating (no feed queue yet):
          - shell ``!`` commands always pass through
          - slash commands pass through unless the name is in ``_AGENT_GATED_COMMANDS``
          - chat text requires the agent to be idle
        Blocked submissions surface as a transient notification.
        """
        if self.state == ChatPaneViewModel.State.COMMIT:
            # In commit mode the input buffer is interpreted as optional commit instructions.
            self.chat_input.accept_submission(text)
            self.submit_commit_payload(text)
            return

        stripped = text.lstrip()

        if stripped.startswith("!"):
            cmd = stripped[1:].strip()
            if cmd:
                self.chat_input.accept_submission(text)
                self.start_shell_command(cmd)
            return

        if stripped.startswith("/"):

            name = stripped.lstrip("/").split(maxsplit=1)[0]
            if self.agent_busy and name in self._AGENT_GATED_COMMANDS:
                self.emit(self.notify, ChatPaneViewModel.NotifyAction.AGENT_BUSY)
                return

            self.chat_input.accept_submission(text)
            self._schedule_worker(self._execute_command(stripped))
            return

        if self.agent_busy:
            self.emit(self.notify, ChatPaneViewModel.NotifyAction.AGENT_BUSY)
            return

        self.chat_input.accept_submission(text)
        self.start_agent_run(text)

    def start_shell_command(self, cmd: str) -> None:
        """Append a shell-command VM to the feed and kick off its execute() on the worker scheduler.
        Unlike agent runs, shell commands aren't gated by ``agent_busy`` — they're side-channel to
        the conversation.
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        vm = ShellCommandViewModel(cmd)
        self._append_feed(vm)
        self._schedule_worker(vm.execute())


    # ------------------------------------------------------------------
    # Commit mode
    # ------------------------------------------------------------------
    #
    # State machine: CONVERSATION ↔ COMMIT. Most public API asserts CONVERSATION; the methods below
    # are the only legal way in and out of COMMIT. Selection state lives on each
    # ``AgentMessageViewModel`` (its own dirty drives the per-message border + checkbox); the pane
    # holds the ordered snapshot of selectable VMs and the cursor index so navigation is O(1).
    #
    # ``submit_commit_payload`` is stubbed: it cleans up decoration and returns to CONVERSATION but
    # doesn't yet build/forward the payload to the agent — that wiring (and the routing decision
    # between direct/subagent paths) lands in a follow-up.

    _COMMIT_HINT = "Type instructions for the commit (Enter to submit, may be empty)..."

    def enter_commit_mode(self) -> None:
        """Snapshot learn-mode agent messages, decorate them as the selectable set, and transition to
        COMMIT. If no learn-mode agent messages exist, append a system message and stay in
        CONVERSATION.
        """
        assert self.state == ChatPaneViewModel.State.CONVERSATION
        assert not self.agent_busy

        selectable = [
            item.entry for item in self.feed
            if isinstance(item.entry, AgentMessageViewModel) and item.entry.mode == Mode.LEARN
        ]
        if not selectable:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content="No selectable messages to commit."),
                include_in_agent_context=False,
            )
            return

        self._commit_selectable = selectable
        self._commit_cursor = 0
        for i, vm in enumerate(selectable):
            vm.set_selectable(True)
            vm.set_cursor(i == 0)

        self.state = ChatPaneViewModel.State.COMMIT
        self.chat_input.set_hint(self._COMMIT_HINT)
        self.chat_input.set_state(ChatInputViewModel.State.COMMIT)
        # Move focus off the input so up/down/enter drive the cursor rather than the input's
        # history nav / submit. The view's focus subscription routes this to the message-area
        # scroll container; events bubble back to the pane's commit-mode bindings.
        self.request_focus()
        self.emit(self.dirty)


    def navigate_commit_cursor_up(self) -> None:
        assert self.state == ChatPaneViewModel.State.COMMIT
        self._move_commit_cursor(-1)


    def navigate_commit_cursor_down(self) -> None:
        assert self.state == ChatPaneViewModel.State.COMMIT
        self._move_commit_cursor(1)


    def _move_commit_cursor(self, delta: int) -> None:
        new_index = self._commit_cursor + delta
        if new_index < 0 or new_index >= len(self._commit_selectable):
            return
        self._commit_selectable[self._commit_cursor].set_cursor(False)
        self._commit_cursor = new_index
        self._commit_selectable[new_index].set_cursor(True)


    def toggle_include_current_message_in_commit(self) -> None:
        """Toggle the message under the cursor. On select (not deselect), auto-advance the cursor
        to the next selectable if there is one.
        """
        assert self.state == ChatPaneViewModel.State.COMMIT

        if not self._commit_selectable:
            return
        
        current = self._commit_selectable[self._commit_cursor]
        was_selected = current.is_selected
        current.set_selected(not was_selected)

        # Auto-advance logic
        if not was_selected and self._commit_cursor < len(self._commit_selectable) - 1:
            self._move_commit_cursor(1)


    def exit_commit_mode(self) -> None:
        """Cancel commit mode without submitting. Clears all decoration and returns to CONVERSATION."""
        assert self.state == ChatPaneViewModel.State.COMMIT
        self._reset_commit_state()


    def submit_commit_payload(self, instructions: str) -> None:
        """Submit the commit payload (selected messages + optional free-text instructions) and return
        to CONVERSATION.

        Builds a payload from the selected ``AgentMessageViewModel`` bodies (each annotated with the
        immediately-preceding USER message as ``user_context``), injects it into the agent session,
        posts a system notification with the direct-vs-subagent routing hint, and kicks off an
        agent-only turn (no USER message in the feed — the notification is the prompt).

        Edge case: if the user submitted with zero selected messages, exit COMMIT and append
        "No messages selected for commit." Mirrors the legacy ``confirm_commit_selection`` behavior.
        """
        assert self.state == ChatPaneViewModel.State.COMMIT

        payload = self._build_commit_payload()

        # Transition out of COMMIT first — every subsequent feed mutation / agent kick-off requires
        # CONVERSATION state, and we never re-enter COMMIT from inside this method.
        self._reset_commit_state()

        if not payload:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content="No messages selected for commit."),
                include_in_agent_context=False,
            )
            return

        if self.agent_session is None:
            self.append_message(
                ChatMessageData(role=Role.ERROR, content="Agent session not bootstrapped."),
            )
            return

        self.agent_session.set_commit_payload(payload)
        self.agent_session.add_system_notification(
            self._build_commit_notification(payload, instructions),
        )
        self._start_agent_turn()

    def _build_commit_payload(self) -> list[dict]:
        """Walk ``_commit_selectable`` (in feed order) and build one dict per selected message:

            {"index": int, "content": str, "user_context": str | None}

        ``user_context`` is the most recent USER ``ChatMessageData`` preceding this agent message
        in the feed, bailing on an earlier ``AgentMessageViewModel`` so we capture the immediate
        prompt rather than stale conversation. Messages with empty bodies are skipped.
        """
        payload: list[dict] = []
        for idx, vm in enumerate(self._commit_selectable):
            if not vm.is_selected or vm.is_empty:
                continue
            entry: dict = {"index": idx, "content": vm.body}
            user_context = self._preceding_user_context(vm)
            if user_context is not None:
                entry["user_context"] = user_context
            payload.append(entry)
        return payload

    def _preceding_user_context(self, vm: AgentMessageViewModel) -> str | None:
        """Scan backwards in the feed from ``vm``'s position for the nearest USER ChatMessageData.
        Stop and return None if we hit an earlier AgentMessageViewModel first — that means the
        prompt for this segment is somewhere upstream we shouldn't conflate.
        """
        feed_pos = next((i for i, item in enumerate(self.feed) if item.entry is vm), None)
        if feed_pos is None:
            return None
        for item in reversed(self.feed[:feed_pos]):
            entry = item.entry
            if isinstance(entry, AgentMessageViewModel):
                return None
            if isinstance(entry, ChatMessageData) and entry.role == Role.USER:
                return entry.content
        return None

    def _build_commit_notification(self, payload: list[dict], instructions: str) -> str:
        """Compose the system notification handed to the agent. Routing (direct vs subagent) follows
        ``Options.Subagents.Commit.*``; this matches the legacy chat pane's behavior verbatim. The
        subagent path will eventually move to the agent session itself — see the design discussion
        in this turn — but for now the pane owns the decision so we don't touch the session API.
        """
        combined = "\n".join(entry["content"] for entry in payload)
        approx_tokens = count_tokens_approximately([HumanMessage(content=combined)])
        num_messages = len(payload)

        use_subagent = False
        if self._options is not None and self._options.get(Options.Subagents.Commit.Enabled) == "enabled":
            criterion = self._options.get(Options.Subagents.Commit.RoutingCriterion)
            threshold = self._options.get(Options.Subagents.Commit.RoutingThreshold)
            if criterion == "tokens":
                use_subagent = approx_tokens >= threshold
            else:
                use_subagent = num_messages >= threshold

        if use_subagent:
            notification = (
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_invoke_subagent to delegate "
                "knowledge entry extraction, then present the proposal to the user."
            )
        else:
            notification = (
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_show_selected_messages and "
                "commit_proposal_create to draft entries directly, then present "
                "the proposal to the user."
            )

        if instructions:
            notification += (
                f"\n\nUser provided these additional instructions for the commit:\n{instructions}"
            )
        return notification


    def _reset_commit_state(self) -> None:
        for vm in self._commit_selectable:
            vm.clear_commit_decoration()

        self.state = ChatPaneViewModel.State.CONVERSATION
        self._commit_selectable = []
        self._commit_cursor = 0

        self.chat_input.set_state(ChatInputViewModel.State.CHAT)
        self.chat_input.reset_hint()
        self.chat_input.request_focus()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Command registry
    # ------------------------------------------------------------------

    def _registry_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for name, cmd in self._command_registry.commands.items():
            desc = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
            desc = desc.strip().splitlines()[0] if desc else ""
            rows.append((name, desc))
        return rows


    async def _execute_command(self, text: str) -> None:
        line = text.lstrip("/").strip()
        if not line:
            return

        try:
            result = await self._command_registry.execute(line)
        except KeyError as exc:
            self.append_message(ChatMessageData(role=Role.ERROR, content=str(exc).strip("'")))
            return
        except Exception as exc:  # noqa: BLE001 — surface unexpected handler errors as ERROR messages
            self.append_message(ChatMessageData(role=Role.ERROR, content=f"Command error: {exc}"))
            return

        if result is not None:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=str(result), rich=True),
                include_in_agent_context=False,
            )


    def _register_commands(self) -> None:
        reg = self._command_registry

        @reg.command(name="clear", help="Clear the message feed.")
        def _clear() -> None:
            self.clear_feed()

        @reg.command(name="commit", help="Enter commit mode to select learn-mode messages.")
        def _commit() -> None:
            self.enter_commit_mode()

        @reg.command(name="idle", help="Switch to idle mode.")
        async def _idle() -> None:
            await self.set_mode(Mode.IDLE)

        @reg.command(name="learn", help="Switch to learn mode.")
        async def _learn() -> None:
            await self.set_mode(Mode.LEARN)

        @reg.command(name="review", help="Switch to review mode.")
        async def _review() -> None:
            await self.set_mode(Mode.REVIEW)

        @reg.command(name="echo", help="Echo arguments back as a system message.")
        @click.argument("words", nargs=-1)
        def _echo(words: tuple[str, ...]) -> None:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=" ".join(words) if words else ""),
                include_in_agent_context=False,
            )

        @reg.command(name="test-turn", help="Run a synthetic agent turn to exercise routing.")
        async def _test_turn() -> None:
            await self._run_synthetic_turn()

        @reg.command(
            name="test-flow",
            help=(
                "Stream → pause (try typing!) → interrupt → resume. "
                "Exercises mid-stream input + interrupt teardown."
            ),
        )
        async def _test_flow() -> None:
            await self._run_synthetic_flow()

        @reg.command(name="test-interrupt", help="Spawn a synthetic interrupt to exercise routing.")
        async def _test_interrupt() -> None:
            interrupt = TestInterruptViewModel(prompt="Pick an option:", options=["alpha", "beta", "gamma"])
            result = await self.present_interrupt(interrupt)
            if result is None:
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content="interrupt cancelled"),
                    include_in_agent_context=False,
                )
            else:
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=f"interrupt resolved: {result!r}"),
                    include_in_agent_context=False,
                )

    async def _run_synthetic_turn(self) -> None:
        """Drive the router without invoking the real agent.

        Mounts the thinking indicator via the router's constructor, streams some markdown, emits a
        synthetic tool call, streams more, then closes. Useful as an eyeball test of the
        per-segment routing + delta-streaming.
        """
        router = AgentStreamRouter(self)
        self._current_router = router

        try:
            await asyncio.sleep(2)
            for chunk in ("Sure, let me ", "think about ", "**that** for ", "a moment.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            router.route_tool_call("search_entries", {"query": "mvvm refactor", "limit": 10})
            await asyncio.sleep(0.3)

            router.route_tool_call("list_topics", {})
            await asyncio.sleep(0.3)

            for chunk in ("Here's what I found:\n\n", "- Item one\n", "- Item two\n", "- Item three\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

        finally:
            router.close()
            self._current_router = None

    async def _run_synthetic_flow(self) -> None:
        """End-to-end flow exerciser: streams, pauses long enough for the user to submit something
        mid-stream, emits an interrupt, then resumes in a fresh agent segment. Useful for eyeballing
        the reference-only routing — anything submitted during the pause should land between the
        two segments, while the first stays "open" and keeps receiving chunks.
        """
        router = AgentStreamRouter(self)
        self._current_router = router

        try:
            for chunk in ("Starting a longer turn — ", "I'll pause in a moment ", "so you can chime in.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            router.route_tool_call("search_entries", {"query": "mid-stream", "limit": 5})
            await asyncio.sleep(0.3)

            router.route_chunk("(pausing ~6s — try `/echo hello` or `/learn` now)\n\n")
            await asyncio.sleep(6.0)

            for chunk in ("Back. ", "Now I need to ask you something.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            interrupt = TestInterruptViewModel(
                prompt="Continue with which branch?", options=["left", "right", "neither"],
            )
            result = await self.present_interrupt(interrupt)

            if result is None:
                router.route_chunk("Interrupt cancelled — wrapping up.\n")
            else:
                router.route_chunk(f"Got it: **{result}**. Continuing.\n\n")
                for chunk in ("- step one\n", "- step two\n", "- done\n"):
                    router.route_chunk(chunk)
                    await asyncio.sleep(0.08)

        finally:
            router.close()
            self._current_router = None