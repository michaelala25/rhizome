"""Middleware that swaps the system prompt and filters tools based on the active agent mode.

On every model call, this middleware:

1. Reads the current mode from graph state (``state["mode"]``) and an
   optional pending user-initiated mode change.
2. Idempotently updates the ``SystemMessage`` in graph state (via
   ``abefore_model``) so the conversation history always reflects the
   current mode's prompt.
3. Filters ``request.tools`` to only those allowed by the mode (via
   ``awrap_model_call``, which is stateless).

State modification uses ``before_model`` / ``abefore_model`` because these
hooks return state updates that go through the graph's reducers, persisting
the change in the graph's checkpointed state.  Tool filtering stays in
``wrap_model_call`` / ``awrap_model_call`` because it is a stateless
per-request concern that should not be persisted.

User-initiated mode changes (shift+tab, slash commands) are bridged into
graph state via :meth:`set_pending_user_mode`.  Agent-initiated mode
changes (the ``set_mode`` tool) update graph state directly by returning a
``Command(update={"mode": ...})``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from rhizome.agent_legacy.modes import MODE_REGISTRY, AgentMode
from rhizome.logs import get_logger

_logger = get_logger("agent.middleware.agent_mode")

# Well-known message ID used for the system prompt so the ``add_messages``
# reducer can replace it in-place when the mode changes.
SYSTEM_PROMPT_MESSAGE_ID = "system-prompt"


def _get_tool_name(tool) -> str | None:
    """Extract the name from a BaseTool instance or a server-side tool dict."""
    if hasattr(tool, "name"):
        return tool.name
    if isinstance(tool, dict):
        return tool.get("name")
    return None


def _resolve_mode(mode_name: str, *, debug: bool = False) -> AgentMode:
    """Look up an ``AgentMode`` by name, falling back to idle on unknown names."""
    mode_cls = MODE_REGISTRY.get(mode_name)
    if mode_cls is None:
        _logger.warning("Unknown mode %r — falling back to idle", mode_name)
        mode_cls = MODE_REGISTRY["idle"]
    return mode_cls(debug=debug)


class AgentModeMiddleware(AgentMiddleware):
    """Swap system prompt and filter tools based on the active agent mode.

    System prompt management happens in ``abefore_model`` (state update),
    while tool filtering happens in ``awrap_model_call`` (stateless override).

    The middleware reads the current mode from ``state["mode"]`` (graph state).
    User-initiated mode changes that occur while the agent is streaming are
    bridged into graph state via :meth:`set_pending_user_mode`, which stores
    the pending mode in a single slot consumed on the next ``abefore_model``.
    """

    def __init__(self, *, debug: bool = False) -> None:
        self._debug = debug
        self._lock = asyncio.Lock()
        self._pending_user_mode: str | None = None

    # -- Public API for user-initiated mode changes --------------------------

    async def set_pending_user_mode(self, mode_name: str) -> None:
        """Queue a user-initiated mode change to be applied on the next model call.

        This exists solely to bridge user UI actions (shift+tab, slash
        commands) into graph state while the agent is streaming.  Only the
        latest pending change is retained — rapid successive calls overwrite
        earlier ones.

        Agent-initiated mode changes do NOT go through this path; the
        ``set_mode`` tool updates graph state directly via
        ``Command(update={"mode": ...})``.
        """
        async with self._lock:
            self._pending_user_mode = mode_name

    async def clear_pending_user_mode(self) -> None:
        """Clear any pre-existing user-initiated mode changes.

        This is called at the beginning of every AgentSession.stream call, to clear any latent
        pending mode changes from the prior AgentSession.stream invocation. Each AgentSession.stream
        call supplies a fresh "mode" coming from the app, which is the source of truth for the mode
        in the graph state as well.

        Pending mode changes are only used to support user-initiated mode changes _within_ a currently
        running AgentSession.stream invocation, so that the agent can remain consistent with the app
        state in real time.
        """
        async with self._lock:
            self._pending_user_mode = None

    # -- State update: mode + system message ---------------------------------

    def before_model(self, state, runtime) -> dict[str, Any] | None:
        return self._sync_mode_and_prompt(state)

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        return self._sync_mode_and_prompt(state)

    def _sync_mode_and_prompt(self, state) -> dict[str, Any] | None:
        """Synchronise graph state's mode and system prompt.

        Two cases:

        1. **Pending user mode change** — the user changed mode via the UI
           while the agent was streaming.  Apply the new mode to graph state
           and inject a notification message so the agent knows.
        2. **No pending change** — check whether the system prompt for the
           current ``state["mode"]`` is stale (covers agent-initiated
           changes where the ``set_mode`` tool updated ``state["mode"]``
           via ``Command`` but the system prompt hasn't been refreshed yet).
        """
        # NOTE: We read/clear the pending slot without the async lock here
        # because _sync_mode_and_prompt is called from both sync and async
        # hooks.  The slot is a single atomic assignment (protected by the
        # GIL), and the async lock in set_pending_user_mode prevents
        # concurrent writes from the UI side.
        pending = self._pending_user_mode
        if pending is not None:
            self._pending_user_mode = None

        current_mode_name: str = state.get("mode", "idle")

        if pending is not None and pending != current_mode_name:
            # User-initiated mode change — apply + notify the agent.
            mode = _resolve_mode(pending, debug=self._debug)
            _logger.info("Applying pending user mode change: %s -> %s", current_mode_name, pending)
            return {
                "mode": pending,
                "messages": [
                    SystemMessage(content=mode.system_prompt, id=SYSTEM_PROMPT_MESSAGE_ID),
                    HumanMessage(content=f"[System] The user has changed the mode to: {pending}."),
                ],
            }

        # No pending user change — ensure the system prompt matches state["mode"].
        mode = _resolve_mode(current_mode_name, debug=self._debug)
        prompt = mode.system_prompt

        messages = state.get("messages", [])
        existing = _find_system_message(messages)

        if existing is not None and _system_message_content(existing) == prompt:
            return None  # Already up to date.

        _logger.debug("Updating system message in state for mode %r", current_mode_name)
        msg_id = existing.id if (existing is not None and existing.id) else SYSTEM_PROMPT_MESSAGE_ID
        return {
            "messages": [SystemMessage(content=prompt, id=msg_id)],
        }

    # -- Stateless override: tool filtering ----------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._filter_tools(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return await handler(self._filter_tools(request))

    def _filter_tools(self, request: ModelRequest) -> ModelRequest:
        """Filter request tools to those allowed by the current mode."""
        mode_name: str = request.state.get("mode", "idle")
        mode = _resolve_mode(mode_name, debug=self._debug)

        filtered_tools = [
            t for t in request.tools
            if mode.is_tool_allowed(_get_tool_name(t) or "")
        ]
        if len(filtered_tools) != len(request.tools):
            return request.override(tools=filtered_tools)
        return request


# -- Helpers -----------------------------------------------------------------

def _find_system_message(messages) -> SystemMessage | None:
    """Find the system message by well-known ID, falling back to the first one."""
    existing = None
    for msg in messages:
        if isinstance(msg, SystemMessage):
            if msg.id == SYSTEM_PROMPT_MESSAGE_ID:
                return msg
            if existing is None:
                existing = msg
    return existing


def _system_message_content(msg: SystemMessage) -> str:
    """Extract text content from a SystemMessage regardless of content format."""
    content = msg.content
    if isinstance(content, str):
        return content
    # Content can be a list of blocks (e.g. with cache_control added).
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content
        )
    return str(content)
