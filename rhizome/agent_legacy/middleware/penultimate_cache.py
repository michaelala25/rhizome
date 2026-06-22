"""Middleware that places an Anthropic cache_control breakpoint on the
penultimate message.

Anthropic's prompt caching works on a prefix basis.  By marking the
penultimate message with a ``cache_control`` breakpoint, everything before it
becomes a stable, cacheable prefix — even as the last message changes each
turn.

Usage::

    middleware = AnthropicPenultimateCacheMiddleware(ttl="5m")
"""

from __future__ import annotations

from typing import Callable, Literal

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from rhizome.logs import get_logger


class AnthropicPenultimateCacheMiddleware(AgentMiddleware):
    """Applies ``cache_control`` to the penultimate message on every model call.

    Args:
        ttl: TTL for the cache control block. Sets cache control type to
            ``"ephemeral"`` with the given TTL. Ignored if ``cache_control``
            is provided.
        cache_control: Anthropic cache-control descriptor applied to the
            penultimate message. Defaults to 5-minute ephemeral caching.
    """

    DEFAULT_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral", "ttl": "5m"}

    def __init__(
        self,
        *,
        ttl: Literal["5m", "1h"] | None = None,
        cache_control: dict[str, str] | None = None,
    ) -> None:
        if ttl is not None:
            cache_control = {"type": "ephemeral", "ttl": ttl}
        self._cache_control = cache_control or self.DEFAULT_CACHE_CONTROL
        self._logger = get_logger("agent.middleware.penultimate_cache")

    # -- Middleware hook -------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        messages = self._prepare_messages(request)
        return handler(request.override(messages=messages))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        messages = self._prepare_messages(request)
        return await handler(request.override(messages=messages))

    # -- Internals ------------------------------------------------------------

    def _prepare_messages(self, request: ModelRequest) -> list:
        """Build the modified message list for a request."""
        messages = list(request.messages)

        if len(messages) >= 2:
            self._logger.debug(
                "Injecting cache control breakpoint (messages=%d)",
                len(messages),
            )
            try:
                messages[-2] = self._with_cache_control(messages[-2])
            except Exception:
                import traceback
                self._logger.error(
                    "Failed to add cache control: %s",
                    traceback.format_exc(),
                )

        return messages

    def _with_cache_control(self, msg):
        """Return a copy of *msg* with ``cache_control`` on its content."""
        self._logger.debug(
            "_with_cache_control - msg type: %s - content type: %s",
            type(msg),
            type(msg.content),
        )

        content = msg.content
        if isinstance(content, str):
            content = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": self._cache_control,
                }
            ]
        elif isinstance(content, list):
            content = list(content)
            last_block = dict(content[-1])
            last_block["cache_control"] = self._cache_control
            content[-1] = last_block

        return msg.__class__(content=content, **{
            k: v for k, v in msg.__dict__.items() if k != "content"
        })
