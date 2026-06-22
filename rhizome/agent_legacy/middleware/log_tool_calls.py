"""Middleware that logs tool call invocations with their arguments."""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from rhizome.logs import get_logger

_logger = get_logger("agent.tool_calls")


class LogToolCallsMiddleware(AgentMiddleware):
    """Log every tool invocation at DEBUG level, including arguments."""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        name = request.tool_call.get("name", "<unknown>")
        args = request.tool_call.get("args", {})
        _logger.debug("Tool call: %s(%s)", name,
                       ", ".join(f"{k}={v!r}" for k, v in args.items()))
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        name = request.tool_call.get("name", "<unknown>")
        args = request.tool_call.get("args", {})
        _logger.debug("Tool call: %s(%s)", name,
                       ", ".join(f"{k}={v!r}" for k, v in args.items()))
        return await handler(request)
