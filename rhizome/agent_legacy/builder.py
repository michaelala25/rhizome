"""Agent graph builder — provider-agnostic wrapper around create_agent/init_chat_model.

Two entry points:

- ``build_agent`` — generic builder for subagents and simple use cases.
- ``build_root_agent`` — extends ``build_agent`` with mode middleware, prompt
  caching, web tools, and other root-agent-specific features.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from rhizome.agent_legacy.config import get_api_key
from rhizome.agent_legacy.context import AgentContext
from rhizome.agent_legacy.middleware import (
    LogToolCallsMiddleware,
    AgentModeMiddleware,
    AnthropicPenultimateCacheMiddleware,
    DisableParallelToolCallsMiddleware,
)

from rhizome.agent_legacy.state import RhizomeAgentState
from rhizome.logs import get_logger

_logger = get_logger("agent")


def _init_model(provider: str, model_name: str, temperature: float = 0.3):
    """Create a ``BaseChatModel`` for the given provider."""
    if provider == "anthropic":
        return init_chat_model(model_name, api_key=get_api_key(), temperature=temperature)
    raise ValueError(f"Unsupported provider: {provider}")


def build_agent(
    tools: list,
    provider: str,
    model_name: str,
    *,
    name: str | None = None,
    response_format=None,
    middleware: list | None = None,
    context_schema=None,
    state_schema=None,
    **kwargs,
):
    """Build a model + compiled agent graph.

    This is the generic builder used by subagents and any other lightweight
    agent.  ``LogToolCallsMiddleware`` is always prepended to the middleware
    chain.

    Returns a ``(model, agent, all_middleware)`` tuple.
    """
    tag = f" ({name})" if name else ""
    _logger.info("Building agent%s (provider=%s, model=%s)", tag, provider, model_name)

    model = _init_model(provider, model_name, temperature=kwargs.get("temperature", 0.3))

    all_middleware: list[AgentMiddleware] = [LogToolCallsMiddleware()]
    if middleware:
        all_middleware.extend(middleware)

    create_kwargs: dict = dict(
        model=model,
        tools=list(tools),
        middleware=all_middleware,
        response_format=response_format,
        context_schema=context_schema,
        checkpointer=InMemorySaver(),
    )
    if state_schema is not None:
        create_kwargs["state_schema"] = state_schema

    agent = create_agent(**create_kwargs)
    return model, agent, all_middleware


def build_root_agent(
    tools: list,
    provider: str,
    model_name: str,
    **agent_kwargs,
):
    """Build the root agent with mode switching, prompt caching, and web tools.

    Assembles root-specific middleware and tools, then delegates to
    ``build_agent``.

    Returns a ``(model, agent, middleware_list)`` tuple.
    """

    _logger.info("Building root agent (provider=%s, model=%s)", provider, model_name)

    debug = agent_kwargs.get("debug", False)
    middleware: list[AgentMiddleware] = [AgentModeMiddleware(debug=debug)]

    if not agent_kwargs.get("parallel_tool_calling", True):
        middleware.append(DisableParallelToolCallsMiddleware())

    if agent_kwargs.get("prompt_cache", True):
        ttl = agent_kwargs.get("prompt_cache_ttl", "5m")
        middleware.append(AnthropicPenultimateCacheMiddleware(ttl=ttl))

    all_tools: list = list(tools)
    if agent_kwargs.get("web_tools", False):
        all_tools.append({"name": "web_search", "type": "web_search_20260209", "max_uses": 5})
        all_tools.append({"name": "web_fetch", "type": "web_fetch_20260209", "max_uses": 5})
        _logger.info("Web tools enabled (web_search, web_fetch)")

    return build_agent(
        all_tools,
        provider,
        model_name,
        name="root",
        middleware=middleware,
        context_schema=AgentContext,
        state_schema=RhizomeAgentState,
        temperature=agent_kwargs.get("temperature", 0.3),
    )
