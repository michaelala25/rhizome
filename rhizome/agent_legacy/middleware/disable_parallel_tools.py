"""Middleware that disables parallel tool calls.

Each tool creates its own DB session, so parallel execution is safe
by default.  This middleware exists as a debugging option: it injects
``parallel_tool_calls=False`` into ``model_settings`` so that
``bind_tools`` tells the provider to emit only one tool call per
response.
"""

from __future__ import annotations

from typing import Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)


class DisableParallelToolCallsMiddleware(AgentMiddleware):
    """Set ``parallel_tool_calls=False`` on every model request."""

    def _patched(self, request: ModelRequest) -> ModelRequest:
        settings = {**request.model_settings, "parallel_tool_calls": False}
        return request.override(model_settings=settings)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._patched(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return await handler(self._patched(request))
