"""ChatAreaStreamRouter — translates one run's stream events into feed mutations.

One instance per run, constructed by ``ChatAreaModel.submit`` and handed to ``AgentGraph.stream`` as
the run's ``AgentStreamingContext``. Nobody holds a reference to it afterwards: every lifecycle
moment — interrupts, cancellation, errors, completion — arrives through this object's own callbacks
from inside ``AgentSession.stream``, so there is no external teardown to sequence and no
"current router" slot anywhere.

The router pins the cursor it was launched from. All feed mutations (chat segments, tool lists, the
thinking indicator, cancelled/error messages) target that cursor's leaf through the conversation
graph, so mid-run navigation by the user cannot redirect output into the wrong branch. The pinned
leaf cannot freeze mid-run — ``branch``/``merge`` reject busy parents.

Reference-based routing, not feed position: the open chat segment and tool list are tracked by
reference, so a user message landing mid-stream shoves them away from the tail without breaking the
routing — subsequent chunks still flow into the referenced segment.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from langchain_core.messages import AIMessageChunk

from rhizome.agent.streaming import AgentStreamingContext, RunStateView
from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.chat_pane.interrupts.flashcard_review import FlashcardReviewInterruptModel
from rhizome.app.chat_pane.interrupts.multi_choices import MultiUserChoicesModel
from rhizome.app.chat_pane.interrupts.sql import SqlConfirmationModel
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_pane.interrupts.warning import WarningUserChoicesModel
from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.app.chat_pane.messages.tool import ToolMessageModel
from rhizome.app.chat_pane.thinking import ThinkingIndicatorModel
from rhizome.tui.types import Mode, Role

from .conversation_graph import Cursor

if TYPE_CHECKING:
    from .chat_area import ChatAreaModel


# Maps the ``"type"`` key on a graph interrupt-value dict to the VM factory building the matching
# feed entry. The agent graph emits these strings, so we follow rather than rename them. The VM
# classes are bridge-imported from ``chat_pane`` — they're view-models slated to move here wholesale
# when the old pane retires.
_INTERRUPT_VM_FACTORIES: dict[str, Callable[[dict[str, Any]], InterruptModelBase]] = {
    "choices": UserChoicesModel.from_interrupt,
    "warning": WarningUserChoicesModel.from_interrupt,
    "multiple_choice": MultiUserChoicesModel.from_interrupt,
    "sql_confirmation": SqlConfirmationModel.from_interrupt,
}


class ChatAreaStreamRouter(AgentStreamingContext):

    def __init__(self, area: "ChatAreaModel", cursor: Cursor) -> None:
        self._area = area
        self._graph = area.conversation_graph
        self._cursor = cursor

        # Mode tag for new chat segments (commit-mode selection + border styling read it later).
        # Tracked live from the run's RunStateView via _refresh_mode — a mode payload ingested at
        # the run's first model call tags that same run's segments.
        self._mode = Mode.IDLE

        # The open chat segment / tool list, tracked by reference. Cleared on transitions and pause.
        self._current_agent_message: AgentMessageModel | None = None
        self._current_tool_message: ToolMessageModel | None = None

        # Feed id of the thinking indicator; repinned (remove + re-append, fresh id) on each new
        # segment so it stays beneath the latest visible content. Mounted eagerly — constructing a
        # router IS starting a run.
        self._thinking_id: int | None = None
        self._show_thinking()

        self._had_output = False
        self._cancelled = False

    # ------------------------------------------------------------------
    # Stream callbacks (driven by AgentSession.stream)
    # ------------------------------------------------------------------

    async def on_message(self, payload: Any, state: RunStateView) -> None:
        """Extract text deltas from ``AIMessageChunk`` payloads into the open chat segment.
        Non-text chunks (``input_json_delta`` etc.) are ignored so they don't open a segment."""
        self._refresh_mode(state)
        chunk, _meta = payload
        if not isinstance(chunk, AIMessageChunk):
            return
        if not chunk.text:
            return
        self.route_chunk(chunk.text)

    async def on_update(self, payload: dict, state: RunStateView) -> None:
        """Route tool calls from update payloads into the open tool list.

        Client tool calls come from the provider-neutral ``msg.tool_calls`` (langchain normalizes
        them there regardless of how the provider shapes content). Server-side tool blocks never
        reach ``tool_calls``, so the content scan picks those up — skipping Anthropic's
        ``server_tool_use(code_execution)`` wrapper, whose wrapped tool appears as its own block.
        """
        self._refresh_mode(state)

        # One updates event = {graph_step_name: channels_that_step_wrote} — one entry per step
        # that ran (middleware hooks included). New messages arrive under each step's "messages".
        for update in payload.values():
            if not isinstance(update, dict):
                continue

            for msg in update.get("messages", []):
                # Client tool calls: langchain lifts these out of provider-specific content into
                # ``AIMessage.tool_calls`` as [{"name", "args", "id"}, ...].
                for tc in getattr(msg, "tool_calls", None) or []:
                    if tc.get("name"):
                        self.route_tool_call(tc["name"], tc.get("args") or {})

                # Server-side tools execute provider-side and never reach ``tool_calls`` — they
                # exist only as {"type": "server_tool_use", "name", "input"} content blocks.
                # Code-execution helpers arrive doubled: a "code_execution" wrapper block plus the
                # actual tool as its own block, so the wrapper is skipped.
                content = getattr(msg, "content", None)
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "server_tool_use":
                        continue
                    name = block.get("name")
                    if name and name != "code_execution":
                        self.route_tool_call(name, block.get("input") or {})

        self._refresh_usage(state)

    async def on_interrupt(self, value: Any, agent_context: Any, state: RunStateView) -> Any:
        """Seal the open segment, surface the interrupt through the chat area, and resume the stream
        with whatever it resolves to. The thinking indicator hides for the wait and returns after —
        including when the wait dies to cancellation (the cancel path tears it down via close())."""
        self._refresh_mode(state)
        self.pause()
        self._hide_thinking()
        try:
            return await self._area.present_interrupt(
                self._build_interrupt_vm(value, agent_context), cursor=self._cursor
            )
        finally:
            self._show_thinking()

    async def on_cancelled(self) -> None:
        """Fired from inside the session's CancelledError handler, after history repair. Freeze the
        open segment where it stands and leave the visible trace."""
        self._cancelled = True
        if self._current_agent_message is not None:
            self._current_agent_message.mark_cancelled()
        self._area.append_message("(user cancelled)", Role.SYSTEM, cursor=self._cursor)

    async def on_exception(self, exc: BaseException) -> None:
        self._area.append_message(f"Agent error: {exc}", Role.ERROR, cursor=self._cursor)

    async def on_complete(self, state: RunStateView) -> None:
        """Always fires (success, cancel, or error) — the single teardown point. The busy-flip
        event is not ours: the agent node fires it at the worker pinpoint, right after this."""
        self._refresh_mode(state)
        self._refresh_usage(state)
        self.close()

    def _refresh_usage(self, state: RunStateView) -> None:
        """Recompute this branch's token-usage report from the run's evolving state and cache it on the
        node. Computed once per ``updates`` event — the cadence the prefix total actually changes on, so
        no need to recompute per token chunk.

        Only the *visible* branch drives the shared status bar: a background turn updates its own node's
        cache without clobbering the bar the user is looking at; a later cursor move re-reads that cache
        (see ``ChatAreaModel._sync_status_bar``)."""
        node = self._graph.node(self._cursor)
        report = node.session.engine.report(state.values)
        node.usage_report = report
        if self._area.cursor == self._cursor:
            self._area.status_bar.set_usage_report(report)

    def _refresh_mode(self, state: RunStateView) -> None:
        """Track the run's mode as state evolves, so segments opened after a mid-run mode change
        (and the "(no response)" stub) carry the right tag."""
        try:
            self._mode = Mode(state.get("mode", Mode.IDLE.value))
        except ValueError:
            self._mode = Mode.IDLE

    # ------------------------------------------------------------------
    # Segment lifecycle
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Seal the open chat segment and drop both segment references; the next chunk opens a
        fresh segment. The thinking indicator is left alone."""
        if self._current_agent_message is not None:
            self._current_agent_message.close()
            self._current_agent_message = None
        self._current_tool_message = None

    def close(self) -> None:
        """Finalize the run: pause + remove the thinking indicator. Safe to call multiple times.
        A run with no visible output gets a "(no response)" stub — except a cancelled one, whose
        "(user cancelled)" message (posted by on_cancelled) is the appropriate trace."""
        self.pause()
        self._hide_thinking()

        if self._cancelled:
            return

        if not self._had_output:
            stub = AgentMessageModel(mode=self._mode)
            stub.body = "(no response)"
            stub.streaming = False
            self._graph.append(self._cursor, stub)
            self._had_output = True

    # ------------------------------------------------------------------
    # Routing primitives (also drivable by synthetic test commands)
    # ------------------------------------------------------------------

    def route_chunk(self, text: str) -> None:
        """Route a streamed text delta into the open chat segment, opening a fresh one (and closing
        any open tool list) if needed."""
        if self._current_tool_message is not None:
            self._current_tool_message = None

        if self._current_agent_message is None or not self._current_agent_message.streaming:
            vm = AgentMessageModel(mode=self._mode)
            self._current_agent_message = vm
            self._graph.append(self._cursor, vm)
            self._repin_thinking()
            self._had_output = True

        self._current_agent_message.append_token(text)

    def route_tool_call(self, name: str, args: dict | None = None) -> None:
        """Route a tool call into the open tool list, opening a fresh one (and sealing any open chat
        segment) if needed."""
        if self._current_agent_message is not None:
            self._current_agent_message.close()
            self._current_agent_message = None

        if self._current_tool_message is None:
            vm = ToolMessageModel()
            self._current_tool_message = vm
            self._graph.append(self._cursor, vm)
            self._repin_thinking()
            self._had_output = True

        self._current_tool_message.add_tool_call(name, args)

    @staticmethod
    def _build_interrupt_vm(value: Any, context: Any) -> InterruptModelBase:
        """Translate a graph interrupt-value dict into the matching feed-resident VM. Raises on a
        missing/unknown ``"type"`` — a typo or not-yet-ported interrupt should surface loudly."""
        if not isinstance(value, dict):
            raise ValueError(
                f"Interrupt value must be a dict with a 'type' key, got {type(value).__name__}"
            )
        itype = value.get("type")
        if itype == "flashcard_review":
            # The one interrupt that needs the run context: the review widget pulls the agent runtime
            # (its auto-scorer's handle) and the DB session factory off it.
            return FlashcardReviewInterruptModel.from_interrupt(value, context)
        factory = _INTERRUPT_VM_FACTORIES.get(itype)
        if factory is None:
            raise ValueError(f"Unknown interrupt type: {itype!r}")
        return factory(value)

    # ------------------------------------------------------------------
    # Thinking indicator
    # ------------------------------------------------------------------

    def _show_thinking(self) -> None:
        if self._thinking_id is not None:
            return
        self._thinking_id = self._graph.append(self._cursor, ThinkingIndicatorModel()).id

    def _hide_thinking(self) -> None:
        if self._thinking_id is None:
            return
        self._graph.remove(self._cursor, self._thinking_id)
        self._thinking_id = None

    def _repin_thinking(self) -> None:
        if self._thinking_id is None:
            return
        self._hide_thinking()
        self._show_thinking()
