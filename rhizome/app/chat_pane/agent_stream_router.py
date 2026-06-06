"""AgentStreamRouter — translates agent stream events into chat-pane feed mutations.

The router is **ephemeral**: one instance per agent turn, constructed at the start of
``_run_agent_turn`` and discarded when the worker's finally block runs ``close()``. The pane holds
a transient ``_current_router`` slot during the turn so the interrupt path can reach it; outside
of a turn that slot is ``None``.

The router owns the routing of an agent turn's output into the feed:
  * ``AgentMessageModel`` entries for streamed text (one per contiguous chat segment),
  * ``ToolMessageModel`` entries for tool calls (one per contiguous run between chat segments),
  * a ``ThinkingIndicatorModel`` that lives in the feed as its own entry and gets repinned to
    the tail whenever a new segment is appended, so it always sits beneath the latest visible
    content for the duration of the turn.

Lifecycle: construct → ``on_message`` / ``on_update`` route stream events (or ``route_chunk`` /
``route_tool_call`` from synthetic tests) → ``pause()`` from the interrupt path → ``close()`` at
turn end. The indicator is mounted by the constructor; there is no separate ``start()``.

Mutation back-channel
---------------------
The router mutates the feed by calling ``pane._append_feed`` / ``pane._remove_feed`` on the
back-reference passed at construction. It reads ``pane.session_mode`` to seed new
``AgentMessageModel`` instances with the right mode for border styling. No other coupling to
the pane.

Reference-based routing (not feed position)
-------------------------------------------
The currently-open chat segment and tool list are tracked by *reference*, not by being at the tail
of the feed. A user message or ``/command`` can land mid-stream and shove the open segment away
from the tail; subsequent chunks still route into the referenced segment, not whatever the new
tail happens to be. (Step 1's feed-by-id makes the indicator's remove+re-append cheap because the
mounted-widgets index is a dict, not a list.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from langchain_core.messages import AIMessageChunk

from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.chat_pane.interrupts.multi_choices import MultiUserChoicesModel
from rhizome.app.chat_pane.interrupts.sql import SqlConfirmationModel
from rhizome.app.chat_pane.thinking import ThinkingIndicatorModel
from rhizome.app.chat_pane.messages.tool import ToolMessageModel
from rhizome.app.chat_pane.interrupts.warning import WarningUserChoicesModel


# Maps the ``"type"`` key on a graph interrupt-value dict to the VM factory that builds the matching
# feed entry. Keys mirror the legacy ``agent_message_harness`` dispatch table verbatim — the agent
# graph emits these strings, so we follow rather than rename them. New interrupt VMs hook in here.
_INTERRUPT_VM_FACTORIES: dict[str, Callable[[dict[str, Any]], InterruptModelBase]] = {
    "choices": UserChoicesModel.from_interrupt,
    "warning": WarningUserChoicesModel.from_interrupt,
    "multiple_choice": MultiUserChoicesModel.from_interrupt,
    "sql_confirmation": SqlConfirmationModel.from_interrupt,
}


if TYPE_CHECKING:
    from .conversation_graph import ConversationGraphCursor
    from .view_model import ChatPaneModel


class AgentStreamRouter:

    def __init__(self, pane: "ChatPaneModel") -> None:
        self._pane = pane

        # Cursor snapshot at turn start. All feed mutations made by this router (append agent
        # message, append tool list, append/remove thinking indicator, present interrupt) target
        # this cursor's leaf rather than ``pane._cursor``, so mid-turn navigation by the user
        # cannot redirect agent output into the wrong branch. Cursors are frozen tuples, so
        # holding the reference is safe — the pane reassigns the *attribute* when navigating, our
        # captured snapshot is unaffected. The pinned leaf stays open throughout the turn because
        # ``/branch`` is gated on ``agent_busy``.
        self._cursor: "ConversationGraphCursor" = pane._cursor

        # The currently-open agent chat segment, if any. Cleared on tool-call transition or by
        # ``pause`` / ``close``.
        self._current_agent_message: AgentMessageModel | None = None

        # The currently-open tool list, if any. Same reference-tracking rules. Tool messages have
        # no ``streaming`` flag — "open" just means new tool calls extend this VM rather than
        # opening a fresh one.
        self._current_tool_message: ToolMessageModel | None = None

        # Feed id of the thinking indicator. The indicator is its own feed entry; it gets repinned
        # to the tail (remove + re-append, with a fresh id) on each new segment so it stays at the
        # bottom of the visible run. Mounted eagerly here — constructing a router *is* starting a
        # turn.
        self._thinking_id: int | None = None
        self._show_thinking()

        # Whether the turn has produced any visible output. If still False at ``close`` we
        # synthesize a stub "(no response)" agent message so empty turns still leave a visible
        # trace.
        self._had_output: bool = False

    # ------------------------------------------------------------------
    # Turn lifecycle (called by the pane)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Seal the current chat segment and drop both segment references, but leave the thinking
        indicator mounted. Called by the pane before presenting an interrupt — the agent is still
        busy, just awaiting user input. On resume, the next chunk opens a fresh segment.
        """
        if self._current_agent_message is not None:
            self._current_agent_message.close()
            self._current_agent_message = None
        self._current_tool_message = None

    def close(self, *, cancelled: bool = False) -> None:
        """Finalize the turn: pause + remove the thinking indicator. Safe to call multiple times.

        ``cancelled=False`` (the default — natural end of stream): if no visible output was
        produced, append a "(no response)" stub so empty turns leave a trace.

        ``cancelled=True`` (user-initiated cancel, via ``ChatPaneModel.cancel_agent_turn``):
        mark the currently-open agent message cancelled *before* sealing it, so the view's drain
        loop bails on its next iteration instead of slowly catching up to the buffered body. Then
        skip the stub — the worker's ``CancelledError`` handler posts a "(user cancelled)"
        system message which is the appropriate visible trace; doubling up with "(no response)"
        is noise. Closed-but-still-draining previous segments aren't signalled here: by
        construction they were sealed when the next segment opened, so their drain budgets are
        the snappy 200ms tail and they finish within a few ticks regardless.
        """
        if cancelled and self._current_agent_message is not None:
            self._current_agent_message.mark_cancelled()

        self.pause()
        self._hide_thinking()

        if cancelled:
            return

        if not self._had_output:
            stub = AgentMessageModel(mode=self._pane.session_mode)
            stub.body = "(no response)"
            stub.streaming = False
            self._pane._append_feed(stub, cursor=self._cursor)
            self._had_output = True

    # ------------------------------------------------------------------
    # Stream callbacks (wired into agent_session.stream by the pane)
    # ------------------------------------------------------------------

    async def on_message(self, kind: str, payload: Any) -> None:
        """``messages`` stream callback: extract text deltas from ``AIMessageChunk`` payloads and
        route them as chat-segment chunks. Non-text chunks (``input_json_delta`` etc.) are ignored
        so they don't start a chat segment for them.
        """
        chunk, _meta = payload
        if not isinstance(chunk, AIMessageChunk):
            return
        if not chunk.text:
            return
        self.route_chunk(chunk.text)

    async def on_update(self, kind: str, payload: dict) -> None:
        """``updates`` stream callback: walk the update payload, pull tool_use block names, and
        route them as tool-call entries on the open tool list. Anthropic's internal
        ``server_tool_use(code_execution)`` wrapper is skipped — the actual tool appears as its
        own block.
        """
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
                    if btype == "server_tool_use" and block.get("name") == "code_execution":
                        continue
                    if btype not in ("tool_use", "server_tool_use"):
                        continue

                    name = block.get("name")
                    if not name:
                        continue

                    args = block.get("input") or {}
                    self.route_tool_call(name, args)

    async def on_interrupt(self, value: Any, context: Any) -> Any:
        self.pause()
        self._hide_thinking()
        try:
            return await self._pane.present_interrupt(
                self._build_interrupt_vm(value), cursor=self._cursor,
            )
        finally:
            self._show_thinking()

    @staticmethod
    def _build_interrupt_vm(value: Any) -> InterruptModelBase:
        """Translate a graph interrupt-value dict into the matching feed-resident VM.

        Raises on a missing/unknown ``"type"`` rather than falling back silently — a typo in the
        graph or a not-yet-ported interrupt should surface as an explicit error, not a generic
        placeholder.
        """
        if not isinstance(value, dict):
            raise ValueError(
                f"Interrupt value must be a dict with a 'type' key, got {type(value).__name__}"
            )
        itype = value.get("type")
        factory = _INTERRUPT_VM_FACTORIES.get(itype)
        if factory is None:
            raise ValueError(f"Unknown interrupt type: {itype!r}")
        return factory(value)

    # ------------------------------------------------------------------
    # Routing primitives (also called by synthetic test commands)
    # ------------------------------------------------------------------

    def route_chunk(self, text: str) -> None:
        """Route a streamed text delta into the open chat segment, opening a fresh one and closing
        any preceding tool list if the current head isn't a chat segment.
        """
        if self._current_tool_message is not None:
            self._current_tool_message = None

        if self._current_agent_message is None or not self._current_agent_message.streaming:
            vm = AgentMessageModel(mode=self._pane.session_mode)
            self._current_agent_message = vm
            self._pane._append_feed(vm, cursor=self._cursor)
            self._repin_thinking()
            self._had_output = True

        self._current_agent_message.append_token(text)

    def route_tool_call(self, name: str, args: dict | None = None) -> None:
        """Route a tool call into the open tool list, opening a fresh one and closing any preceding
        chat segment if the current head isn't a tool list.
        """
        if self._current_agent_message is not None:
            self._current_agent_message.close()
            self._current_agent_message = None

        if self._current_tool_message is None:
            vm = ToolMessageModel()
            self._current_tool_message = vm
            self._pane._append_feed(vm, cursor=self._cursor)
            self._repin_thinking()
            self._had_output = True

        self._current_tool_message.add_tool_call(name, args)

    # ------------------------------------------------------------------
    # Thinking indicator (private)
    # ------------------------------------------------------------------

    def _show_thinking(self) -> None:
        if self._thinking_id is not None:
            return
        item = self._pane._append_feed(ThinkingIndicatorModel(), cursor=self._cursor)
        self._thinking_id = item.id

    def _hide_thinking(self) -> None:
        if self._thinking_id is None:
            return
        self._pane._remove_feed(self._thinking_id, cursor=self._cursor)
        self._thinking_id = None

    def _repin_thinking(self) -> None:
        if self._thinking_id is None:
            return
        self._hide_thinking()
        self._show_thinking()
