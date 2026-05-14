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
from typing import Any, TYPE_CHECKING

import rich_click as click

from ..view_model_base import ViewModelBase
from rhizome.agent.session import AgentSession, get_agent_kwargs
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


class ChatPaneViewModel(ViewModelBase):

    class Callbacks(Enum):
        FEED_APPEND = "feed_append"
        FEED_CLEAR = "feed_clear"

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ) -> None:
        super().__init__()

        self._feed_append = self._make_group(ChatPaneViewModel.Callbacks.FEED_APPEND)
        self._feed_clear = self._make_group(ChatPaneViewModel.Callbacks.FEED_CLEAR)

        self.feed: list[FeedEntry] = []

        self.input_enabled: bool = True
        self.input_hint: str = _DEFAULT_HINT
        self.input_buffer: str = ""

        self.session_mode: Mode = Mode.IDLE

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
            chat_pane=None,
            resource_manager=self.resource_manager,
            provider=provider,
            model_name=model_name,
            agent_kwargs=agent_kwargs,
            debug=debug,
        )

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def append_message(self, msg: ChatMessageData) -> None:
        """Append a message to the feed, applying the consecutive-system dedup rule.

        If the previous feed entry is a system message with identical content, we
        suppress the append; otherwise the message is appended and ``feed_append``
        fires with the new index. Does **not** close any open agent turn — see
        "Feed ordering rules" above ``open_agent_turn``.
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
            return await vm.wait_for_selection()
        except asyncio.CancelledError:
            return None
        finally:
            self._pending_interrupt = None
            self.input_enabled = prev_enabled
            self.input_hint = prev_hint
            self.emit(self.dirty)


    # ------------------------------------------------------------------
    # Session mode
    # ------------------------------------------------------------------

    def set_session_mode(self, mode: Mode) -> None:
        """Set the session mode and announce the change as a system message.

        Step 2 only handles the user-initiated, agent-idle branch of the
        legacy transition matrix; the agent_busy / source=agent branches
        come in with the agent session in step 3.
        """
        if self.session_mode == mode:
            self.append_message(ChatMessageData(
                role=Role.SYSTEM, content=f"Already in {mode.value} mode."
            ))
            return

        self.session_mode = mode
        if mode == Mode.IDLE:
            text = "Returned to idle mode."
        else:
            text = f"Entered {mode.value} mode."

        self.append_message(ChatMessageData(role=Role.SYSTEM, content=text, mode=mode))

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

        # TODO(feed-queue): once ``agent_run_state`` lands, this branches:
        #   - dispatchable now (slash/shell command, or chat text with the
        #     agent idle) → run immediately. Output appends after the open
        #     agent VM if one exists; the agent keeps streaming into it.
        #   - chat text with the agent running → enqueue on a feed-queue
        #     for drain after the current run terminates.
        # We don't have a real "can we handle this now?" predicate yet
        # (no run-state, no shell commands), so everything currently runs
        # immediately.

        if text.lstrip().startswith("/"):
            self.set_user_input_buffer("")
            asyncio.create_task(self._execute_command(text.lstrip()))
            return

        self.append_message(ChatMessageData(role=Role.USER, content=text))
        self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"echo: {text}"))

        self.set_user_input_buffer("")


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
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=str(result), rich=True))


    def _register_commands(self) -> None:
        reg = self._command_registry

        @reg.command(name="clear", help="Clear the message feed.")
        def _clear() -> None:
            self.clear_feed()

        @reg.command(name="idle", help="Switch to idle mode.")
        def _idle() -> None:
            self.set_session_mode(Mode.IDLE)

        @reg.command(name="learn", help="Switch to learn mode.")
        def _learn() -> None:
            self.set_session_mode(Mode.LEARN)

        @reg.command(name="review", help="Switch to review mode.")
        def _review() -> None:
            self.set_session_mode(Mode.REVIEW)

        @reg.command(name="echo", help="Echo arguments back as a system message.")
        @click.argument("words", nargs=-1)
        def _echo(words: tuple[str, ...]) -> None:
            self.append_message(ChatMessageData(
                role=Role.SYSTEM, content=" ".join(words) if words else ""
            ))

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
                self.append_message(ChatMessageData(
                    role=Role.SYSTEM, content="interrupt cancelled"
                ))
            else:
                self.append_message(ChatMessageData(
                    role=Role.SYSTEM, content=f"interrupt resolved: {result!r}"
                ))

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
