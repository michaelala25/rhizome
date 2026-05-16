"""ChatPaneViewModel — steps 1–3 of the chat-pane MVVM rewrite.

Steps 1+2 cover the feed + commands; step 3 adds an ``AgentSession``
instance, held but unused. No worker, no streaming, no harness yet —
this is just the bootstrap seam.

Out of scope: starting/cancelling agent runs, sub-VMs in the feed,
status-bar projection, shell ``!`` commands, agent gating of commands,
the agent-busy half of the mode-transition matrix.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Literal, TYPE_CHECKING

import rich_click as click
from langchain_core.messages import AIMessageChunk

from ..view_model_base import ViewModelBase
from rhizome.agent.session import AgentSession, get_agent_kwargs
from rhizome.db import Topic
from rhizome.db.operations import get_topic
from rhizome.resources.manager import ResourceManager
from rhizome.tui.commands import CommandRegistry
from rhizome.tui.options import Options
from rhizome.tui.types import ChatMessageData, Mode, Role
from .agent_message import AgentMessageViewModel
from .command_palette import CommandPaletteViewModel
from .interrupt import InterruptViewModelBase, TestInterruptViewModel


FeedEntry = ChatMessageData | AgentMessageViewModel | InterruptViewModelBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


_DEFAULT_HINT = "Type a message or /command ..."


class RunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class ChatPaneViewModel(ViewModelBase):

    class Callbacks(Enum):
        FEED_APPEND = "feed_append"
        FEED_CLEAR = "feed_clear"
        TAB_RENAME = "tab_rename"
        VERBOSITY_HINT = "verbosity_hint"

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ) -> None:
        super().__init__()

        self._feed_append = self._make_group(ChatPaneViewModel.Callbacks.FEED_APPEND)
        self._feed_clear = self._make_group(ChatPaneViewModel.Callbacks.FEED_CLEAR)
        self._tab_rename = self._make_group(ChatPaneViewModel.Callbacks.TAB_RENAME)
        self._verbosity_hint = self._make_group(ChatPaneViewModel.Callbacks.VERBOSITY_HINT)

        self.feed: list[FeedEntry] = []

        self.input_enabled: bool = True
        self.input_hint: str = _DEFAULT_HINT
        self.input_buffer: str = ""

        self.session_mode: Mode = Mode.IDLE

        # Active topic + path from the topic tree root. Mutated via
        # set_topic / clear_topic; surfaced by the view in the status bar.
        self.active_topic: Topic | None = None
        self.topic_path: list[str] = []

        self.command_palette = CommandPaletteViewModel()
        self._command_registry = CommandRegistry()
        self._register_commands()
        self.command_palette.set_commands(self._registry_rows())

        # Agent plumbing — instantiated on bootstrap (after the view has access
        # to app.options). Held but unused at step 3.
        self._session_factory = session_factory
        self.resource_manager: ResourceManager | None = (
            ResourceManager(session_factory=session_factory) if session_factory else None
        )
        self.agent_session: AgentSession | None = None

        # The currently-open AgentMessage VM at the feed tail, if any. Set by
        # ``open_agent_turn``; cleared whenever a non-AgentMessage entry is
        # appended (peek-tail rule) or ``close_agent_turn`` runs.
        self._current_agent_message: AgentMessageViewModel | None = None

        # The interrupt VM currently awaiting user input, if any. Tracked so
        # external callers (eventually: a cancel-during-agent-run path) can
        # resolve it from outside the awaiting coroutine.
        self._pending_interrupt: InterruptViewModelBase | None = None

        # Agent run state — bones only. CANCELLING is reserved for when we
        # wire up explicit cancellation; for now we just flip IDLE↔RUNNING.
        self.agent_run_state: RunState = RunState.IDLE
        self._agent_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Callback group accessors
    # ------------------------------------------------------------------

    @property
    def feed_append(self):
        return self._feed_append

    @property
    def feed_clear(self):
        return self._feed_clear

    @property
    def tab_rename(self):
        return self._tab_rename

    @property
    def verbosity_hint(self):
        return self._verbosity_hint

    @property
    def command_registry(self) -> CommandRegistry:
        return self._command_registry

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap_agent_session(
        self,
        app_options: Options,
        *,
        debug: bool = False,
    ) -> None:
        """Construct ``self.agent_session`` from app options. Idempotent: a
        second call is a no-op. Caller is the view's ``on_mount`` because the
        provider/model values live on Textual's ``app.options``.
        """
        if self.agent_session is not None:
            return
        if self._session_factory is None:
            return

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
            debug=debug,
        )

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def append_message(
        self,
        msg: ChatMessageData,
        *,
        include_in_agent_context: bool = True,
    ) -> None:
        """Append a message to the feed, applying the consecutive-system dedup rule.

        If the previous feed entry is a system message with identical content, we
        suppress the append; otherwise the message is appended and ``feed_append``
        fires with the new index. Does **not** close any open agent turn — see
        "Feed ordering rules" above ``open_agent_turn``.

        When ``include_in_agent_context`` is True (the default), USER messages
        are pushed to the agent via ``add_human_message`` and SYSTEM messages
        via ``add_system_notification`` so they land in the conversation
        history on the next stream. Other roles (ERROR, etc.) are never
        forwarded — they're UI-side noise the agent shouldn't react to.
        Callers wanting feed-only mutation (test commands, UI echoes,
        legacy ``ui_only=True`` cases) pass ``include_in_agent_context=False``.
        """
        tail = self.feed[-1] if self.feed else None
        if (
            msg.role == Role.SYSTEM
            and isinstance(tail, ChatMessageData)
            and tail.role == Role.SYSTEM
            and tail.content == msg.content
        ):
            # TODO: ping the existing entry. Will likely flow through a
            # per-entry dirty once ChatMessageData (or a wrapping sub-VM)
            # owns its own emit channel.
            return

        self.feed.append(msg)
        self.emit(self.feed_append, len(self.feed) - 1)

        if include_in_agent_context and self.agent_session is not None:
            if msg.role == Role.USER:
                self.agent_session.add_human_message(msg.content)
            elif msg.role == Role.SYSTEM:
                self.agent_session.add_system_notification(msg.content)

    def clear_feed(self) -> None:
        if not self.feed:
            return
        
        self.feed.clear()
        self._current_agent_message = None
        self.emit(self.feed_clear)

    # ------------------------------------------------------------------
    # Feed ordering rules
    # ------------------------------------------------------------------
    #
    # The feed is append-only; **position is not identity**. The currently
    # open AgentMessage VM is tracked by the ``_current_agent_message``
    # reference, not by being at the tail.
    #
    # This matters because a user message can land in the feed mid-stream
    # — e.g. a ``/command`` dispatched while the agent is still responding.
    # The open agent VM is no longer trailing the feed, but the agent
    # session keeps streaming into it. (Free-text chat mid-stream is the
    # queued case; see ``submit_user_input``.)
    #
    # Lifecycle:
    #
    # - **Open**: ``open_agent_turn`` creates an AgentMessage VM, appends
    #   it to the feed, and stores it as ``_current_agent_message``.
    #   Idempotent while the VM is still streaming.
    #
    # - **Stream**: ``_route_agent_chunk`` / ``_route_agent_tool_call``
    #   resolve the open VM by *reference*, opening a fresh one only if
    #   there isn't one. They never consult feed position.
    #
    # - **Close**: ``close_agent_turn`` finalizes the VM and clears the
    #   reference. Closure is **explicit** — driven by the agent worker
    #   terminating, or by the interrupt path sealing the current turn
    #   before awaiting user input. Appending an unrelated entry (user
    #   message, system echo, sub-VM) does NOT close the open agent turn.
    #
    # Interrupt sequence:
    #   1. agent emits an interrupt-requesting tool call
    #   2. caller closes the current agent turn explicitly
    #   3. caller appends the interrupt VM and awaits resolution
    #   4. on resolve, a fresh ``open_agent_turn`` starts the next VM

    def open_agent_turn(self) -> AgentMessageViewModel:
        """Eagerly create an AgentMessage VM and append it to the feed so the
        view can render the thinking indicator before any chunks arrive.
        Idempotent while ``_current_agent_message`` is still streaming.
        """
        if (
            self._current_agent_message is not None
            and self._current_agent_message.streaming
        ):
            return self._current_agent_message

        vm = AgentMessageViewModel(mode=self.session_mode)
        self._current_agent_message = vm
        self.feed.append(vm)
        self.emit(self.feed_append, len(self.feed) - 1)

        return vm


    def close_agent_turn(self, *, empty_fallback: str | None = None) -> None:
        """Close the currently-open AgentMessage VM, if any. Safe to call
        multiple times.
        """
        if self._current_agent_message is None:
            return

        self._current_agent_message.close(empty_fallback=empty_fallback)
        self._current_agent_message = None


    def _route_agent_chunk(self, text: str) -> None:
        """Append a streamed text delta to the open AgentMessage VM, opening
        a fresh one if there isn't one."""
        vm = self._ensure_open_agent_message()
        vm.append_token(text)


    def _route_agent_tool_call(self, name: str, args: dict | None = None) -> None:
        """Append a tool call to the open AgentMessage VM, opening a fresh
        one if there isn't one."""
        vm = self._ensure_open_agent_message()
        vm.add_tool_call(name, args)


    def _ensure_open_agent_message(self) -> AgentMessageViewModel:
        """Reference-only: return the open AgentMessage VM, or open a fresh
        one. Does not consult feed position — the open VM may sit anywhere
        in the feed if the user has dispatched commands mid-stream."""
        if (
            self._current_agent_message is not None
            and self._current_agent_message.streaming
        ):
            return self._current_agent_message
        return self.open_agent_turn()


    # ------------------------------------------------------------------
    # Interrupts
    # ------------------------------------------------------------------

    async def present_interrupt(self, vm: InterruptViewModelBase) -> Any:
        """Append an interrupt VM to the feed and await its resolution.

        Closes any currently-open agent turn (peek-tail). Disables the chat
        input + swaps in a contextual hint for the duration; restores both on
        resolve/cancel. Returns the resolved value, or ``None`` if cancelled.

        The interrupt VM stays in the feed after resolution as an inert
        record — the View dims it but doesn't remove it, so the conversation
        history reflects what was chosen.
        """
        self.close_agent_turn()
        self.feed.append(vm)
        self._pending_interrupt = vm
        self.emit(self.feed_append, len(self.feed) - 1)

        prev_enabled = self.input_enabled
        prev_hint = self.input_hint
        self.input_enabled = False
        self.input_hint = "Resolve the prompt above to continue..."
        self.emit(self.dirty)

        try:
            return await vm.future()
        except asyncio.CancelledError:
            return None
        finally:
            self._pending_interrupt = None
            self.input_enabled = prev_enabled
            self.input_hint = prev_hint
            self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Agent run lifecycle
    # ------------------------------------------------------------------
    #
    # Bones only: ``start_agent_run`` appends the user message, queues it
    # on the AgentSession, and spawns ``_run_agent_turn`` as an asyncio
    # task. ``_run_agent_turn`` drives ``AgentSession.stream`` with VM-side
    # callbacks that route into the open AgentMessage VM.
    #
    # TODO: worker scheduling belongs to the view (Textual ``run_worker``),
    # not the VM. For now the VM owns the task so the bones are
    # self-contained. Cancellation hooks land with the worker move.

    def start_agent_run(self, user_text: str) -> None:
        """Append the user message and kick off an agent turn. No-op if a
        run is already in flight (real queueing comes with the feed-queue)."""
        if self.agent_run_state is not RunState.IDLE:
            return
        
        if self.agent_session is None:
            self.append_message(ChatMessageData(
                role=Role.ERROR, content="Agent session not bootstrapped.",
            ))
            return

        self.append_message(ChatMessageData(role=Role.USER, content=user_text))

        self._agent_task = asyncio.create_task(self._run_agent_turn())


    async def _run_agent_turn(self) -> None:
        assert self.agent_session is not None

        self.agent_run_state = RunState.RUNNING
        self.emit(self.dirty)
        self.open_agent_turn()

        try:
            await self.agent_session.stream(
                mode=self.session_mode.value,
                on_message=self._on_agent_message,
                on_update=self._on_agent_update,
                on_interrupt=self._on_agent_interrupt,
            )

        except asyncio.CancelledError:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="(user cancelled)"))
            raise

        except Exception as exc:  # noqa: BLE001 — surface stream errors as ERROR messages
            self.append_message(ChatMessageData(role=Role.ERROR, content=f"Agent error: {exc}"))

        finally:
            self.close_agent_turn(empty_fallback="(no response)")
            self.agent_run_state = RunState.IDLE
            self._agent_task = None
            self.emit(self.dirty)


    async def _on_agent_message(self, kind: str, payload: Any) -> None:
        """``messages`` stream callback: append text deltas from AIMessageChunks."""
        chunk, _meta = payload
        if not isinstance(chunk, AIMessageChunk):
            return
        if not chunk.text:
            # input_json_delta chunks etc. have no text — ignore so we don't
            # start a message segment for them.
            return
        self._route_agent_chunk(chunk.text)


    async def _on_agent_update(self, kind: str, payload: dict) -> None:
        """``updates`` stream callback: extract tool_use block names and
        route them as tool-call entries on the open AgentMessage VM."""
        for update in payload.values():
            if update is None:
                continue

            for msg in update.get("messages", []):
                content = getattr(msg, "content", None)
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue

                    btype = block.get("type")
                    # Skip Anthropic's internal code_execution wrapper — the
                    # actual tool (web_fetch etc.) appears as its own block.
                    if btype == "server_tool_use" and block.get("name") == "code_execution":
                        continue
                    if btype not in ("tool_use", "server_tool_use"):
                        continue

                    name = block.get("name")
                    if not name:
                        continue

                    args = block.get("input") or {}
                    self._route_agent_tool_call(name, args)


    async def _on_agent_interrupt(self, value: Any, context: Any) -> Any:
        """Stub: surface the interrupt payload via TestInterrupt so the
        pause/resume path is at least visible. Real per-type dispatch
        (commit/flashcard/choices/etc.) lands with the interrupt registry."""
        interrupt = TestInterruptViewModel(
            prompt=f"(interrupt) {value!r}",
            options=["continue"],
        )
        return await self.present_interrupt(interrupt)


    # ------------------------------------------------------------------
    # Session mode
    # ------------------------------------------------------------------

    async def set_mode(
        self,
        mode: Mode,
        *,
        silent: bool = False,
        source: Literal["user", "agent"] = "user",
    ) -> None:
        """Set the session mode.

        Args:
            mode: target mode.
            silent: suppress the chat system message (shift+tab cycling,
                agent-initiated changes). Forced True when source=="agent".
            source: ``"user"`` (UI-initiated — queues a pending change on
                the agent middleware so graph state catches up on the
                next model call) or ``"agent"`` (tool-initiated — graph
                state is updated directly via ``Command``).
        """
        agent_busy = self.agent_run_state is RunState.RUNNING

        if source == "agent":
            assert agent_busy
            silent = True

        if self.session_mode == mode:
            if not silent:
                self.append_message(ChatMessageData(
                    role=Role.SYSTEM, content=f"Already in {mode.value} mode.",
                ))
            return

        message = (
            "Returned to idle mode." if mode == Mode.IDLE
            else f"Entered {mode.value} mode."
        )

        if source == "agent":
            self.session_mode = mode
            self.emit(self.dirty)
            # User-initiated mode set while the agent was running is
            # superseded by the agent's tool call.
            if self.agent_session is not None:
                await self.agent_session._mode_middleware.clear_pending_user_mode()
            return

        # source == "user"
        if agent_busy:
            self.session_mode = mode
            if self.agent_session is not None:
                await self.agent_session.set_pending_user_mode(mode.value)
            if not silent:
                # Feed-only: set_pending_user_mode handles the agent side
                # when the queue drains.
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=message, mode=mode),
                    include_in_agent_context=False,
                )
        else:
            self.session_mode = mode
            if silent:
                if self.agent_session is not None:
                    self.agent_session.add_system_notification(message)
            else:
                self.append_message(ChatMessageData(
                    role=Role.SYSTEM, content=message, mode=mode,
                ))

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
        self.emit(self.dirty)

    async def set_topic(self, topic_id: int) -> bool:
        """Resolve ``topic_id`` and set it as the active topic. Walks
        parents to build ``topic_path``. Returns False if the topic does
        not exist (state is left unchanged).
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
        self.emit(self.dirty)
        return True

    async def set_tab_name(self, name: str) -> None:
        """Rename the active tab. The VM doesn't own tab state — emits
        through the ``tab_rename`` callback group for the view (or a
        Tabs-level VM) to apply.
        """
        new_name = name.strip()
        if not new_name:
            return
        self.emit(self.tab_rename, new_name)

    def hint_higher_verbosity(self) -> None:
        """Hint to the user that a higher verbosity setting may help.

        TODO: remove this API — the underlying UX cue is on the way out.
        Kept as a no-op-ish emit so the agent tool stays valid.
        """
        self.emit(self.verbosity_hint)

    # ------------------------------------------------------------------
    # Input area
    # ------------------------------------------------------------------

    def set_user_input_buffer(self, text: str) -> None:
        if self.input_buffer == text:
            return
        self.input_buffer = text
        self.command_palette.update_for_input(text)
        self.emit(self.dirty)


    def submit_user_input(self) -> None:
        """Dispatch the current buffer.

        ``/cmd ...`` runs a registered command (async, fire-and-forget);
        anything else falls through to the step-1 user-message + stub
        system echo so plain chat still works while the agent is absent.
        """
        text = self.input_buffer
        if not text.strip():
            return

        # TODO(feed-queue): once shell commands land + we want
        # mid-stream-dispatchable chat text, this branches:
        #   - dispatchable now (slash/shell command, or chat text with the
        #     agent idle) → run immediately. Output appends after the open
        #     agent VM if one exists; the agent keeps streaming into it.
        #   - chat text with the agent running → enqueue on a feed-queue
        #     for drain after the current run terminates.
        # For now: slash commands run anytime, chat text only starts a
        # run when the agent is idle (drops on the floor otherwise).

        if text.lstrip().startswith("/"):
            self.set_user_input_buffer("")
            asyncio.create_task(self._execute_command(text.lstrip()))
            return

        self.set_user_input_buffer("")
        self.start_agent_run(text)


    def move_palette_cursor(self, delta: int) -> None:
        self.command_palette.move_cursor(delta)


    def confirm_palette_selection(self) -> None:
        """Tab-completion: replace the buffer with ``/<selected> ``."""
        name = self.command_palette.selected_command
        if name is None:
            return
        self.set_user_input_buffer(f"/{name} ")


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
            help="Stream → pause (try typing!) → interrupt → resume. Exercises mid-stream input + interrupt teardown.",
        )
        async def _test_flow() -> None:
            await self._run_synthetic_flow()

        @reg.command(name="test-interrupt", help="Spawn a synthetic interrupt to exercise routing.")
        async def _test_interrupt() -> None:
            interrupt = TestInterruptViewModel(
                prompt="Pick an option:",
                options=["alpha", "beta", "gamma"],
            )
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
        """Drive the peek-tail routing without invoking the real agent.

        Opens a turn, streams some markdown, emits a synthetic tool call,
        streams more, then closes. Useful as an eyeball test of the
        AgentMessage view's segment mounting + delta-streaming behavior.
        """        
        self.open_agent_turn()

        await asyncio.sleep(2)
        for chunk in ("Sure, let me ", "think about ", "**that** for ", "a moment.\n\n"):
            self._route_agent_chunk(chunk)
            await asyncio.sleep(0.08)

        self._route_agent_tool_call(
            "search_entries", {"query": "mvvm refactor", "limit": 10}
        )
        await asyncio.sleep(0.3)

        self._route_agent_tool_call("list_topics", {})
        await asyncio.sleep(0.3)

        for chunk in (
            "Here's what I found:\n\n",
            "- Item one\n",
            "- Item two\n",
            "- Item three\n",
        ):
            self._route_agent_chunk(chunk)
            await asyncio.sleep(0.08)

        self.close_agent_turn(empty_fallback="(no response)")

    async def _run_synthetic_flow(self) -> None:
        """End-to-end flow exerciser: streams, pauses long enough for the user
        to submit something mid-stream, emits an interrupt, then resumes in a
        fresh agent turn. Useful for eyeballing the reference-only routing —
        anything submitted during the pause should land between the two agent
        turns, while the first turn stays "open" and keeps receiving chunks.
        """
        self.open_agent_turn()
        for chunk in ("Starting a longer turn — ", "I'll pause in a moment ", "so you can chime in.\n\n"):
            self._route_agent_chunk(chunk)
            await asyncio.sleep(0.08)

        self._route_agent_tool_call("search_entries", {"query": "mid-stream", "limit": 5})
        await asyncio.sleep(0.3)

        self._route_agent_chunk("(pausing ~6s — try `/echo hello` or `/learn` now)\n\n")
        await asyncio.sleep(6.0)

        for chunk in ("Back. ", "Now I need to ask you something.\n\n"):
            self._route_agent_chunk(chunk)
            await asyncio.sleep(0.08)

        interrupt = TestInterruptViewModel(
            prompt="Continue with which branch?",
            options=["left", "right", "neither"],
        )
        result = await self.present_interrupt(interrupt)

        self.open_agent_turn()
        if result is None:
            self._route_agent_chunk("Interrupt cancelled — wrapping up.\n")
        else:
            self._route_agent_chunk(f"Got it: **{result}**. Continuing.\n\n")
            for chunk in ("- step one\n", "- step two\n", "- done\n"):
                self._route_agent_chunk(chunk)
                await asyncio.sleep(0.08)
        self.close_agent_turn(empty_fallback="(no response)")
