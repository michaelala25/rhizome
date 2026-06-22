"""Builder for the root conversation agent — the user-facing agent the chat area mints sessions against.

``build_root_agent`` is the B'-style builder the ``AgentRuntime`` curries: it injects the shared
``checkpointer`` and the ``APIKeyService``, and binds the agent's option dependencies. The split between
snapshot and live options is the whole point of the two binding shapes (see ``factory`` for the contract):

- ``provider`` / ``model`` / ``temperature`` / ``adaptive_thinking`` / ``effort`` / ``web_tools`` are
  ``Annotated[T, spec]`` snapshots — they shape the model and tool set, so a change to any of them rebuilds
  the agent (they form its invalidation set).
- ``parallel_tool_calling`` / ``prompt_cache`` / ``prompt_cache_ttl`` are ``Annotated[OptionRef[T], spec]``
  live handles — behavioral knobs read fresh, so flipping them never rebuilds. Parallel-tool calling is
  honored per model call by ``ParallelToolCallsMiddleware``; the cache knobs are wired but inert for now
  (see the TODO at engine construction).

The builder returns ``(agent, engine)``. To register the declaration on a factory::

    factory.register("root", build=build_root_agent,
                     context_schema=RootAgentContext, state_schema=RootAgentState)

No ``from __future__ import annotations``: the runtime reads this builder's parameter annotations to wire
service / option injection, so they must be real objects, not stringized.
"""

import re
from typing import Annotated, Awaitable, Callable

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model
from langgraph.graph.state import CompiledStateGraph

from rhizome.app.options import OptionRef, Options
from rhizome.credentials import APIKeyService
from rhizome.logs import get_logger

from .checkpointer import AgentCheckpointerService
from .context import RootAgentContext
from .engine import compute_chat_model_max_tokens, PromptCompilerMiddleware, RootPromptEngine
from .factory import AgentFactory
from .prompts import compose_system_prompt
from .state import RootAgentState
from .subagents import register_subagents
from .tools import (
    build_app_tools,
    build_commit_tools,
    build_database_tools,
    build_flashcard_proposal_tools,
    build_guide_tools,
    build_review_tools,
    build_sql_tools,
    render_schema_reference,
)

_logger = get_logger("agent.root")


# Anthropic server-side tools, declared as wire dicts (not LangChain tools), appended when web tools are on.
_WEB_TOOLS = [
    {"name": "web_search", "type": "web_search_20260209", "max_uses": 5},
    {"name": "web_fetch", "type": "web_fetch_20260209", "max_uses": 5},
]

_OPUS_VERSION = re.compile(r"opus-(\d+)-(\d+)")


# ========================================================================================================================
# MIDDLEWARE
# ========================================================================================================================


class ParallelToolCallsMiddleware(AgentMiddleware):
    """Honors the live ``Agent.ParallelToolCalling`` toggle, per model call.

    Holds the option as a live ``OptionRef`` rather than a baked-in snapshot, so flipping it takes effect
    on the next model call without rebuilding the agent. When disabled, it sets ``parallel_tool_calls=False``
    in the request's ``model_settings`` so ``bind_tools`` tells the provider to emit a single tool call per
    turn. A no-op while enabled, so it is installed unconditionally. Async-only, matching the rest of the
    stack (agents run through ``astream`` / ``ainvoke``).
    """

    def __init__(self, toggle: OptionRef[str]) -> None:
        self._toggle = toggle

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._toggle.get() == "disabled":
            request = request.override(model_settings={**request.model_settings, "parallel_tool_calls": False})
        return await handler(request)


# ========================================================================================================================
# BUILDER
# ========================================================================================================================


def _supports_temperature(provider: str, model: str) -> bool:
    """Whether ``temperature`` is accepted for this provider/model. Anthropic dropped the parameter on Opus
    4.7+ (passing it now errors), so the builder omits it there; every other model still honors it."""
    if provider != "anthropic":
        return True
    match = _OPUS_VERSION.search(model)
    return not (match is not None and (int(match.group(1)), int(match.group(2))) >= (4, 7))


def _supports_effort(provider: str, model: str) -> bool:
    """Whether Anthropic's ``effort`` parameter is accepted. Anthropic-only, and the Haiku tier (plus
    Sonnet 4.5 and earlier) errors on it, so the builder omits it there."""
    return provider == "anthropic" and "haiku" not in model


def _root_tools() -> list:
    """The root agent's full tool surface, flattened from the per-domain builders. Each tool reaches its DB
    session / runtime off the agent context at call time, so the builders need no wiring here."""
    groups = (
        build_database_tools(),
        build_sql_tools(),
        build_app_tools(),
        build_guide_tools(),
        build_review_tools(),
        build_flashcard_proposal_tools(),
        build_commit_tools(),
    )
    return [tool for group in groups for tool in group.values()]


def build_root_agent(
    *,
    # Services (injected by service accessor)
    checkpointer: AgentCheckpointerService,
    api_keys:     APIKeyService,
    # Options (injected by OptionService, bound by runtime, changes propagate to agent instance cache invalidation)
    provider:              Annotated[str,   Options.Agent.Provider],
    model:                 Annotated[str,   Options.Agent.Model],
    temperature:           Annotated[float, Options.Agent.Temperature],
    adaptive_thinking:     Annotated[str,   Options.Agent.Anthropic.AdaptiveThinking],
    effort:                Annotated[str,   Options.Agent.Anthropic.Effort],
    # Option refs (injected by OptionService, changes do NOT cause agent instance cache invalidation)
    web_tools:             Annotated[str,   Options.Agent.Anthropic.WebTools],
    parallel_tool_calling: Annotated[OptionRef[str], Options.Agent.ParallelToolCalling],
    prompt_cache:          Annotated[OptionRef[str], Options.Agent.Anthropic.PromptCache],
    prompt_cache_ttl:      Annotated[OptionRef[str], Options.Agent.Anthropic.PromptCacheTTL],
) -> tuple[CompiledStateGraph, RootPromptEngine]:
    """Build the root conversation agent and its prompt engine, returning ``(agent, engine)``.

    Snapshot options bake into the model / tool set; live ``OptionRef`` options are honored on the fly (see
    the module docstring). ``PromptCompilerMiddleware`` is registered LAST so its ``prepare`` wraps closest
    to the model and nothing reorders messages after it.
    """
    _logger.info("Building root agent (provider=%s, model=%s)", provider, model)

    # Anthropic thinking/effort are construction-time model settings, hence snapshot options: a change
    # rebuilds the agent. `thinking={"type": "adaptive"}` lets Claude decide when/how much to reason, and
    # `display: "summarized"` streams reasoning summaries (the chat area's stream router renders them).
    # `effort` is the depth/spend dial, gated like temperature since Haiku (and older Sonnet) reject it.
    model_kwargs: dict = {}
    adaptive = provider == "anthropic" and adaptive_thinking == "enabled"
    if adaptive:
        model_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
    if _supports_effort(provider, model):
        model_kwargs["effort"] = effort
    # Drop temperature whenever thinking is on: extended thinking required it unset and adaptive is assumed
    # the same, so this is defensive (and already a no-op on 4.7+, which rejects temperature outright).
    if _supports_temperature(provider, model) and not adaptive:
        model_kwargs["temperature"] = temperature

    chat_model = init_chat_model(
        model, model_provider=provider, api_key=api_keys.require(provider), **model_kwargs
    )

    tools = _root_tools()
    if provider == "anthropic" and web_tools == "enabled":
        tools.extend(_WEB_TOOLS)

    # The one fixed system prompt + the registry-driven schema reference (the SSOT for table/column names
    # and the filter DSL). Passed to create_agent so it rides every request as the stable ``system_message``
    # — never swapped per mode, never floated by the engine, so it anchors the prefix cache.
    # TODO(debug): thread the app's --debug flag through to here so we can pass compose_system_prompt(
    # debug=True) and append the debug section; it isn't plumbed to the builder yet.
    system_prompt = compose_system_prompt(schema_reference=render_schema_reference())

    # TODO(cache): thread `provider`, `prompt_cache`, and `prompt_cache_ttl` into RootPromptEngine so its
    # `prepare` can place Anthropic cache-control breakpoints — gated on the provider (OpenAI can't take
    # them) and on the live toggle / TTL. They are injected here now as live OptionRefs so flipping cache
    # or its TTL never rebuilds the agent; the engine ignores them until that lands.
    #
    # The system prompt, tool set, and context window are the build-time constants the engine's `report`
    # needs for token accounting (see engine.usage) — fixed for this build, recomputed whenever a snapshot
    # option rebuilds the agent.
    engine = RootPromptEngine(
        system_prompt=system_prompt,
        tools=tools,
        max_input_tokens=compute_chat_model_max_tokens(chat_model),
    )

    agent = create_agent(
        model=chat_model,
        system_prompt=system_prompt,
        tools=tools,
        middleware=[ParallelToolCallsMiddleware(parallel_tool_calling), PromptCompilerMiddleware(engine)],
        context_schema=RootAgentContext,
        state_schema=RootAgentState,
        checkpointer=checkpointer,
    )
    return agent, engine


def build_agent_factory() -> AgentFactory:
    """Descriptor for the app-global ``AgentFactoryService`` — a fresh ``AgentFactory`` with every agent
    kind registered on it. The single place that answers "which agents exist". Registered once at app
    composition (``rhizome/tui/app.py``), so the per-workspace ``AgentRuntime`` injects this one populated
    registry and builds from its declarations: the root conversation agent here, the subagents (commit,
    flashcard validators/scorer) via ``register_subagents``."""
    factory = AgentFactory()
    factory.register(
        "root", build=build_root_agent, context_schema=RootAgentContext, state_schema=RootAgentState
    )
    register_subagents(factory)
    return factory
