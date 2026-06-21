"""Shared fakes for agent_new tests.

Strategy: run the REAL machinery — ``create_agent``, ``PromptCompilerMiddleware``, a shared
``InMemorySaver``, a real session-scoped ``Options`` (no disk: ``flush`` is a no-op below Root) — and fake
only the model. Fake responses encode what the model saw (``provider:<p>|echo:<last human>|seen:<n>``), so
isolation, injection, and rebuild properties are asserted straight off message content.

No ``from __future__ import annotations``: builders carry real annotations so the runtime's service /
option injection can read them.
"""

import asyncio
from typing import Annotated, Any, NamedTuple

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.types import interrupt

from rhizome.agent_new.checkpointer import AgentCheckpointerService, build_checkpointer
from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.factory import AgentFactory, AgentFactoryService
from rhizome.agent_new.payload import MessagePayload, StateUpdatePayload
from rhizome.agent_new.prompt_engine import PromptCompilerMiddleware, PromptEngine
from rhizome.agent_new.runtime import AgentRuntime, AgentRuntimeService
from rhizome.agent_new.state import AgentState
from rhizome.agent_new.streaming import AgentStreamingContext
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.utils.services import ServiceAccessor


# ------------------------------------------------------------------------------------------------
# Scripted models
# ------------------------------------------------------------------------------------------------

class FakeModel(BaseChatModel):
    """Base for scripted models: tool-bindable, with optional artificial latency to force worker
    interleaving in concurrency tests."""

    delay: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeModel":
        return self

    def _respond(self, messages: list) -> AIMessage:
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._respond(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._generate(messages)


class EchoModel(FakeModel):
    """Replies ``echo:<last human content>|seen:<message count>``."""

    def _respond(self, messages: list) -> AIMessage:
        last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return AIMessage(content=f"echo:{last}|seen:{len(messages)}")


class ProviderEchoModel(FakeModel):
    """Replies ``provider:<p>|echo:<last human>|seen:<n>`` — the ``provider`` is baked in at build time,
    so a rebuild on a provider change is witnessable in the response content."""

    provider: str = "?"

    def _respond(self, messages: list) -> AIMessage:
        last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return AIMessage(content=f"provider:{self.provider}|echo:{last}|seen:{len(messages)}")


class ToolThenEchoModel(FakeModel):
    """First call emits one tool call; once any ToolMessage exists, replies ``echo:<last human>`` — so a
    payload ingested between the two model calls (an eager send) shows up in the final echo."""

    tool_name: str = "noop_tool"

    def _respond(self, messages: list) -> AIMessage:
        if any(isinstance(m, ToolMessage) for m in messages):
            last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
            return AIMessage(content=f"echo:{last}")
        return AIMessage(content="", tool_calls=[{"name": self.tool_name, "args": {}, "id": f"tc-{len(messages)}"}])


class ToolOnceModel(FakeModel):
    """First call emits one tool call; once any ToolMessage exists, answers ``after-tool:<its content>`` —
    so the tool's *result* (e.g. a repair patch) is observable in the final response."""

    tool_name: str = "noop_tool"

    def _respond(self, messages: list) -> AIMessage:
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if results:
            return AIMessage(content=f"after-tool:{results[-1].content}")
        return AIMessage(content="", tool_calls=[{"name": self.tool_name, "args": {}, "id": f"tc-{len(messages)}"}])


class BoomModel(FakeModel):
    """Raises on generation — drives the error path (model errors propagate out of astream, unlike
    tool errors, which langgraph captures into ToolMessages)."""

    def _respond(self, messages: list) -> AIMessage:
        raise RuntimeError("boom")


# ------------------------------------------------------------------------------------------------
# Scripted tools
# ------------------------------------------------------------------------------------------------

@tool
async def slow_tool() -> str:
    """Sleeps long enough to be cancelled mid-flight."""
    await asyncio.sleep(30)
    return "never"


@tool
async def noop_tool() -> str:
    """Returns immediately."""
    return "ok"


@tool
def asking_tool() -> str:
    """Interrupts to ask the user something."""
    answer = interrupt("what say you")
    return f"user-said:{answer}"


# ------------------------------------------------------------------------------------------------
# Streaming context
# ------------------------------------------------------------------------------------------------

class CollectingStreamContext(AgentStreamingContext):
    """Records every callback; ``completed`` fires from on_complete (success, cancel, or error)."""

    def __init__(self, interrupt_response: Any = None) -> None:
        self.updates: list[Any] = []
        self.chunks: list[Any] = []
        self.interrupts: list[Any] = []
        self.structured: Any = None
        self.cancelled = False
        self.exception: BaseException | None = None
        self.completed = asyncio.Event()
        self.last_state: Any = None
        self._interrupt_response = interrupt_response

    async def on_update(self, payload, state) -> None:
        self.updates.append(payload)
        self.last_state = state

    async def on_message(self, payload, state) -> None:
        self.chunks.append(payload)
        self.last_state = state

    async def on_interrupt(self, value, agent_context, state) -> Any:
        self.interrupts.append(value)
        self.last_state = state
        return self._interrupt_response

    async def on_cancelled(self) -> None:
        self.cancelled = True

    async def on_exception(self, exc) -> None:
        self.exception = exc

    async def on_structured_response(self, response) -> None:
        self.structured = response

    async def on_complete(self, state) -> None:
        self.last_state = state
        self.completed.set()

    async def wait(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self.completed.wait(), timeout)


# ------------------------------------------------------------------------------------------------
# Builders & container wiring
# ------------------------------------------------------------------------------------------------

def make_build(
    model_factory,
    *,
    tools=(),
    engine_factory=PromptEngine,
    context_schema=RootAgentContext,
    state_schema=None,
):
    """A builder over a fake model: real ``create_agent`` + compiler middleware + injected checkpointer,
    returning ``(agent, engine)``. The only declared dependency is the checkpointer service."""

    def build(*, checkpointer: AgentCheckpointerService):
        engine = engine_factory()
        extra = {"state_schema": state_schema} if state_schema is not None else {}
        agent = create_agent(
            model=model_factory(),
            tools=list(tools),
            middleware=[PromptCompilerMiddleware(engine)],
            context_schema=context_schema,
            checkpointer=checkpointer,
            **extra,
        )
        return agent, engine

    return build


def provider_echo_build(
    *,
    checkpointer: AgentCheckpointerService,
    provider: Annotated[str, Options.Agent.Provider],
):
    """Builder that *injects* the provider option (snapshot) into the model, so a provider change rebuilds
    the agent and the new value is witnessable in responses."""
    engine = PromptEngine()
    agent = create_agent(
        model=ProviderEchoModel(provider=provider),
        tools=[],
        middleware=[PromptCompilerMiddleware(engine)],
        context_schema=RootAgentContext,
        checkpointer=checkpointer,
    )
    return agent, engine


class Harness(NamedTuple):
    runtime: AgentRuntime
    options: Options
    accessor: ServiceAccessor


def make_runtime() -> Harness:
    """A runtime wired through a real ``ServiceAccessor``: real session-scoped ``Options`` (no disk),
    the shared checkpointer, an empty ``AgentFactory``, and the runtime itself. Register agents on
    ``harness.runtime`` via ``register``; mutate options on ``harness.options``; register extra services
    (e.g. a session factory) on ``harness.accessor``."""
    options = Options(OptionScope.Session)
    accessor = ServiceAccessor()
    accessor.register(OptionService, options)
    accessor.register_descriptor(AgentCheckpointerService, build_checkpointer)
    accessor.register(AgentFactoryService, AgentFactory())
    accessor.register_descriptor(AgentRuntimeService, AgentRuntime)
    return Harness(accessor.get(AgentRuntimeService), options, accessor)


def register(runtime: AgentRuntime, key: str, build, *, context_schema=RootAgentContext, **decl) -> None:
    """Register ``build`` under ``key`` on the runtime's factory (friend access for tests)."""
    runtime._factory.register(key, build=build, context_schema=context_schema, **decl)


def build_runtime(
    model_factory, *, tools=(), engine_factory=PromptEngine, state_schema=None, context_schema=RootAgentContext
) -> AgentRuntime:
    """A runtime with a single ``root`` agent over a fake model, returned ready to use — the
    conversation-layer analogue of ``make_runtime`` + ``register`` (drops the ``Harness``)."""
    h = make_runtime()
    register(
        h.runtime,
        "root",
        make_build(
            model_factory, tools=tools, engine_factory=engine_factory,
            context_schema=context_schema, state_schema=state_schema,
        ),
        context_schema=context_schema,
    )
    return h.runtime


# ------------------------------------------------------------------------------------------------
# Convenience
# ------------------------------------------------------------------------------------------------

def user(text: str) -> MessagePayload:
    return MessagePayload(data=text, role=MessagePayload.Role.USER)


def state_update(**values: Any) -> StateUpdatePayload:
    return StateUpdatePayload(data=values)


def ai_contents(state: dict) -> list[str]:
    return [m.content for m in state["messages"] if isinstance(m, AIMessage)]


async def drive(session, *payloads, ctx: CollectingStreamContext | None = None) -> CollectingStreamContext:
    """Stream ``session`` to completion with the given payloads; returns the recording context. For the
    happy path only — error/cancel tests drive ``session.stream`` directly to catch the re-raise."""
    ctx = ctx or CollectingStreamContext()
    await session.stream(ctx, list(payloads) or None)
    return ctx


async def run_turn(graph, at, text: str) -> CollectingStreamContext:
    """Send one user message to a graph node and stream it (via the node's worker) to completion;
    asserts a clean run. The graph analogue of ``drive``."""
    ctx = CollectingStreamContext()
    graph.send(at, user(text))
    graph.stream(at, ctx)
    await ctx.wait()
    assert ctx.exception is None, ctx.exception
    return ctx
