"""Live Anthropic harness: a real runtime over a real, cheap model.

These tests exercise OUR pipeline against the real wire format — not the model's intelligence — so they
use the cheapest model with a tiny ``max_tokens`` and assert *structure* (a response arrives, a structured
payload parses, a repaired history is accepted), never exact content. Gated by ``--live`` +
``ANTHROPIC_API_KEY`` (see the repo-root ``conftest.py``).
"""

from langchain.agents import create_agent

from rhizome.agent_new.checkpointer import AgentCheckpointerService, build_checkpointer
from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.engine import PromptCompilerMiddleware, PromptEngine
from rhizome.agent_new.factory import AgentFactory, AgentFactoryService
from rhizome.agent_new.runtime import AgentRuntime, AgentRuntimeService
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.utils.services import ServiceAccessor

# Reuse the recording stream context and payload helper from the offline fakes.
from ..fakes import CollectingStreamContext, user

LIVE_MODEL = "claude-haiku-4-5"   # cheapest Anthropic option; the pipeline is under test, not the model
MAX_TOKENS = 64

__all__ = [
    "CollectingStreamContext",
    "build_live_runtime",
    "make_live_model",
    "register_live_agent",
    "user",
]


def make_live_model(**kwargs):
    """A real, cheap ``ChatAnthropic`` (reads ``ANTHROPIC_API_KEY`` from the environment). Imported lazily
    so importing this module never depends on a live model being constructable."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=LIVE_MODEL, max_tokens=MAX_TOKENS, **kwargs)


def build_live_runtime() -> AgentRuntime:
    """A runtime wired through the real container (real session-scoped Options, shared checkpointer)."""
    options = Options(OptionScope.Session)
    accessor = ServiceAccessor()
    accessor.register(OptionService, options)
    accessor.register_descriptor(AgentCheckpointerService, build_checkpointer)
    accessor.register(AgentFactoryService, AgentFactory())
    accessor.register_descriptor(AgentRuntimeService, AgentRuntime)
    return accessor.get(AgentRuntimeService)


def register_live_agent(runtime: AgentRuntime, key: str = "root", *, tools=(), response_format=None) -> None:
    """Register a real-model agent: ChatAnthropic + compiler middleware + injected checkpointer, returning
    ``(agent, engine)``. ``response_format`` opts the agent into structured output."""
    engine = PromptEngine()

    def build(*, checkpointer: AgentCheckpointerService):
        extra = {"response_format": response_format} if response_format is not None else {}
        agent = create_agent(
            model=make_live_model(),
            tools=list(tools),
            middleware=[PromptCompilerMiddleware(engine)],
            context_schema=RootAgentContext,
            checkpointer=checkpointer,
            **extra,
        )
        return agent, engine

    runtime._factory.register(key, build=build, context_schema=RootAgentContext)
