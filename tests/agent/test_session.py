"""AgentSession: run lifecycle, config/context safety, eager delivery, concurrency, state view.

Driven on the REAL stack (``create_agent`` + ``PromptCompilerMiddleware`` + shared ``InMemorySaver``) over
fake models, so callback fan-out, payload ingestion, rebuild safety, and thread isolation are observed
through actual runs rather than asserted about internals.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from rhizome.agent.session import AgentSession, InvokeResult
from rhizome.agent.state import RootAgentState
from rhizome.app.options import Options

from .fakes import (
    ai_contents,
    BoomModel,
    CollectingStreamContext,
    drive,
    EchoModel,
    FakeModel,
    make_build,
    make_runtime,
    noop_tool,
    provider_echo_build,
    register,
    slow_tool,
    state_update,
    ToolThenEchoModel,
    user,
)


def _echo_session(model_factory=EchoModel, **build_kw) -> AgentSession:
    h = make_runtime()
    register(h.runtime, "root", make_build(model_factory, **build_kw))
    return h.runtime.new("root")


# ------------------------------------------------------------------------------------------------
# Stream lifecycle: success, cancel, error
# ------------------------------------------------------------------------------------------------

async def test_stream_happy_path_fires_callbacks():
    ctx = await drive(_echo_session(), user("hi"))
    assert ctx.updates and ctx.chunks                 # both updates and message events arrived
    assert ctx.exception is None and not ctx.cancelled
    assert ctx.completed.is_set()                     # on_complete ran


async def test_cancel_fires_callbacks_repairs_and_reraises():
    h = make_runtime()
    register(h.runtime, "root", make_build(lambda: ToolThenEchoModel(tool_name="slow_tool"), tools=[slow_tool]))
    s = h.runtime.new("root")

    ctx = CollectingStreamContext()
    task = asyncio.create_task(s.stream(ctx, [user("go")]))
    await asyncio.sleep(0.2)                           # let the run reach slow_tool
    assert s.busy
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ctx.cancelled
    assert ctx.completed.is_set()                      # on_complete still runs (finally)
    # The dangling tool_use left by the cancelled tool was repaired into a valid history.
    state = await s.agent_state
    tool_results = [m for m in state["messages"] if m.__class__.__name__ == "ToolMessage"]
    assert tool_results, "repair should have synthesized a tool_result for the orphaned tool call"


async def test_error_fires_on_exception_and_reraises():
    s = _echo_session(BoomModel)
    ctx = CollectingStreamContext()
    with pytest.raises(RuntimeError, match="boom"):
        await s.stream(ctx, [user("x")])
    assert isinstance(ctx.exception, RuntimeError)
    assert ctx.completed.is_set()


# ------------------------------------------------------------------------------------------------
# Config & context safety
# ------------------------------------------------------------------------------------------------

async def test_runnable_config_override_rejects_thread_id():
    s = _echo_session()
    with pytest.raises(ValueError, match="thread_id"):
        await s.invoke([user("x")], runnable_config_override={"configurable": {"thread_id": "elsewhere"}})
    # A benign override (no thread_id) is accepted.
    result = await s.invoke([user("ok")], runnable_config_override={"recursion_limit": 5})
    assert result.response is not None


def test_update_context_guards_and_applies():
    s = _echo_session()

    s._busy = True
    with pytest.raises(RuntimeError):
        s.update_context(hooks=("x",))               # no edits while a run is in flight
    s._busy = False

    for framework_field in ("pending", "runtime"):
        with pytest.raises(ValueError):
            s.update_context(**{framework_field: object()})   # framework-owned, off-limits

    s.update_context(hooks=("x",))
    assert s.agent_context.hooks == ("x",)
    assert s._queue is s.agent_context.pending       # queue re-pinned to the (unchanged) pending


# ------------------------------------------------------------------------------------------------
# Option-driven rebuilds around a live session
# ------------------------------------------------------------------------------------------------

async def test_option_change_midrun_is_safe():
    h = make_runtime()
    register(h.runtime, "prov", provider_echo_build)
    s = h.runtime.new("prov")

    class FlipOnce(CollectingStreamContext):
        def __init__(self, options: Options) -> None:
            super().__init__()
            self._options = options
            self._flipped = False

        async def on_update(self, payload, state) -> None:
            await super().on_update(payload, state)
            if not self._flipped:                    # invalidate mid-run (drops the build cache)
                self._flipped = True
                self._options.set(Options.Agent.Provider, "openai")

    ctx = FlipOnce(h.options)
    await s.stream(ctx, [user("hi")])
    assert ctx.exception is None
    # The in-flight run kept the agent captured at dispatch (anthropic), unaffected by the rebuild.
    assert any("provider:anthropic" in c for c in ai_contents(await s.agent_state))
    # The next run picks up the rebuilt (openai) agent.
    assert "provider:openai" in (await s.invoke([user("again")])).response.content


async def test_rebuild_is_witnessed_across_runs():
    h = make_runtime()
    register(h.runtime, "prov", provider_echo_build)
    s = h.runtime.new("prov")

    assert "provider:anthropic" in (await s.invoke([user("one")])).response.content
    h.options.set(Options.Agent.Provider, "openai")
    assert "provider:openai" in (await s.invoke([user("two")])).response.content


# ------------------------------------------------------------------------------------------------
# Eager mid-run delivery
# ------------------------------------------------------------------------------------------------

async def test_eager_payload_is_consumed_within_the_current_run():
    h = make_runtime()
    register(h.runtime, "root", make_build(lambda: ToolThenEchoModel(tool_name="noop_tool"), tools=[noop_tool]))
    s = h.runtime.new("root")

    class EagerOnce(CollectingStreamContext):
        def __init__(self, session: AgentSession) -> None:
            super().__init__()
            self._session = session
            self._sent = False

        async def on_update(self, payload, state) -> None:
            await super().on_update(payload, state)
            if not self._sent:                       # post mid-run, before the post-tool model call
                self._sent = True
                self._session.send(user("eager!"), eager=True)

    await s.stream(EagerOnce(s), [user("go")])
    # The post-tool model call ingested the eager payload, so it echoes "eager!", not "go".
    assert any(c == "echo:eager!" for c in ai_contents(await s.agent_state))


async def test_eager_payload_lands_after_tool_result_never_orphaning():
    """The ordering that protects the Anthropic tool_use/tool_result pair: the queue is drained only at
    before_model (compile), which runs AFTER the tool node — so an eager payload posted *during* a tool
    call lands as a message AFTER the tool_result, never wedged between the tool_use and its result."""
    h = make_runtime()
    register(h.runtime, "root", make_build(lambda: ToolThenEchoModel(tool_name="noop_tool"), tools=[noop_tool]))
    s = h.runtime.new("root")

    class EagerOnToolUse(CollectingStreamContext):
        def __init__(self, session):
            super().__init__()
            self._s = session
            self._sent = False

        async def on_update(self, payload, state):
            await super().on_update(payload, state)
            saw_tool_use = any(
                isinstance(m, AIMessage) and m.tool_calls
                for upd in payload.values() if isinstance(upd, dict)
                for m in upd.get("messages", [])
            )
            if saw_tool_use and not self._sent:        # post the eager send while the tool is executing
                self._sent = True
                self._s.send(user("eager!"), eager=True)

    await s.stream(EagerOnToolUse(s), [user("go")])

    msgs = (await s.agent_state)["messages"]
    ai_idx = next(i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(msgs[ai_idx + 1], ToolMessage)        # tool_result immediately follows the tool_use
    eager_idx = next(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage) and m.content == "eager!")
    assert eager_idx > ai_idx + 1                           # the eager message landed AFTER the tool_result


# ------------------------------------------------------------------------------------------------
# Concurrency: same agent key, different sessions, no cross-talk
# ------------------------------------------------------------------------------------------------

async def test_concurrent_runs_same_key_are_isolated():
    h = make_runtime()
    register(h.runtime, "root", make_build(lambda: EchoModel(delay=0.05)))   # latency forces interleave
    s1, s2 = h.runtime.new("root"), h.runtime.new("root")

    r1, r2 = await asyncio.gather(s1.invoke([user("one")]), s2.invoke([user("two")]))
    assert "echo:one" in r1.response.content
    assert "echo:two" in r2.response.content

    # Each thread's checkpoint holds only its own human message.
    humans1 = [m.content for m in (await s1.agent_state)["messages"] if isinstance(m, HumanMessage)]
    humans2 = [m.content for m in (await s2.agent_state)["messages"] if isinstance(m, HumanMessage)]
    assert humans1 == ["one"] and humans2 == ["two"]


# ------------------------------------------------------------------------------------------------
# RunStateView
# ------------------------------------------------------------------------------------------------

async def test_run_state_view_folds_scalars_and_messages():
    s = _echo_session(state_schema=RootAgentState)
    # The StateUpdatePayload's mode lands in the first compile update; the view folds it before completion.
    ctx = await drive(s, state_update(mode="learn"), user("hi"))
    view = ctx.last_state
    assert view.get("mode") == "learn"
    # messages are folded through add_messages (the checkpoint's own reducer): the user turn and the
    # echo response are both present in the view by completion, so engine.report(view.values) can run.
    contents = [m.content for m in view.values["messages"]]
    assert "hi" in contents and any("echo:hi" in c for c in contents)


# ------------------------------------------------------------------------------------------------
# Payload staging (idle backlog vs live queue)
# ------------------------------------------------------------------------------------------------

def test_send_routes_between_backlog_and_live_queue():
    s = _echo_session()
    p = user("x")

    # Idle: everything waits in the backlog, eager or not.
    s.send(p)
    s.send(p, eager=True)
    assert len(s.queued) == 2 and not s._queue

    # Busy + consume_live: eager goes straight to the live queue.
    s._busy, s._consume_live = True, True
    s.send(p, eager=True)
    assert len(s._queue) == 1

    # Busy without consume_live (invoke-style): eager defers to the backlog.
    s._consume_live = False
    s.send(p, eager=True)
    assert len(s._queue) == 1 and len(s.queued) == 3


# ------------------------------------------------------------------------------------------------
# Invoke result, resume, concurrent-run guard, structured-response extraction
# ------------------------------------------------------------------------------------------------

async def test_invoke_returns_result_and_resumes_via_get():
    h = make_runtime()
    register(h.runtime, "root", make_build(EchoModel))
    s = h.runtime.new("root")

    result = await s.invoke([user("hi")])
    assert isinstance(result, InvokeResult)
    assert result.thread_id == s.thread_id
    assert result.response.content == "echo:hi|seen:1"
    assert result.structured_response is None        # plain prose parses to nothing

    resumed = h.runtime.get("root", result.thread_id)
    assert resumed is s                              # the runtime owns and hands back the same session
    assert (await resumed.invoke([user("again")])).response.content == "echo:again|seen:3"


async def test_invoke_rejects_a_second_in_flight_run():
    s = _echo_session()
    s._busy = True
    with pytest.raises(RuntimeError):
        await s.invoke([user("nope")])
    s._busy = False


def test_extract_structured_response_paths():
    extract = AgentSession._extract_structured_response
    # Documented path wins when present.
    assert extract({"structured_response": {"a": 1}}) == {"a": 1}
    # Fallbacks: JSON string content, then list-of-blocks content with a text block.
    assert extract({"messages": [AIMessage(content='{"score": 4}')]}) == {"score": 4}
    assert extract({"messages": [AIMessage(content=[{"type": "text", "text": '{"ok": true}'}])]}) == {"ok": True}
    # Prose / empty -> None.
    assert extract({"messages": [AIMessage(content="Hello!")]}) is None
    assert extract({"messages": []}) is None


# ------------------------------------------------------------------------------------------------
# Subagent invocation from inside a tool — the production delegation pattern
# ------------------------------------------------------------------------------------------------

@tool
async def consult(text: str, runtime: ToolRuntime, conversation_id: str | None = None) -> str:
    """Consult a subagent, optionally continuing a prior conversation. Reaches the runtime through the
    agent context (``ctx.runtime``); ``new`` for a fresh thread, ``get`` to resume an emitted one."""
    rt = runtime.context.runtime
    sub = rt.get("sub", conversation_id) if conversation_id else rt.new("sub")
    result = await sub.invoke([user(text)])
    return f"{result.response.content}@{result.thread_id}"


class DelegatingModel(FakeModel):
    """Calls ``consult`` for each human turn, threading the subagent conversation id parsed from the most
    recent tool result; relays the tool result as its final answer."""

    def _respond(self, messages: list) -> AIMessage:
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content=f"relay:{messages[-1].content}")
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        conversation_id = tool_results[-1].content.rsplit("@", 1)[1] if tool_results else None
        human = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "consult",
                "args": {"text": human, "conversation_id": conversation_id},
                "id": f"tc-{len(messages)}",
            }],
        )


async def test_tool_invokes_subagent_and_continues_its_conversation():
    h = make_runtime()
    register(h.runtime, "root", make_build(DelegatingModel, tools=[consult]))
    register(h.runtime, "sub", make_build(EchoModel))
    root = h.runtime.new("root")

    # Turn 1: the tool provisions a fresh subagent conversation mid-run (re-entrant invoke over the shared
    # checkpointer) and emits its thread id.
    r1 = await root.invoke([user("alpha")])
    assert r1.response.content.startswith("relay:echo:alpha|seen:1@"), r1.response.content
    sub_thread = r1.response.content.rsplit("@", 1)[1]
    assert sub_thread != root.thread_id

    # Turn 2: the model threads the emitted id back through the tool args; ``runtime.get`` resumes the SAME
    # subagent session, which REMEMBERS — it saw alpha, its reply, then beta (seen:3).
    r2 = await root.invoke([user("beta")])
    assert r2.response.content.startswith("relay:echo:beta|seen:3@"), r2.response.content
    assert r2.response.content.rsplit("@", 1)[1] == sub_thread

    # The subagent thread is independently reachable through the runtime that owns it, and holds exactly
    # the two consultations — no root turns. Isolation holds both directions.
    sub = h.runtime.get("sub", sub_thread)
    assert [m.content for m in (await sub.agent_state)["messages"] if isinstance(m, HumanMessage)] == ["alpha", "beta"]
    assert [m.content for m in (await root.agent_state)["messages"] if isinstance(m, HumanMessage)] == ["alpha", "beta"]


async def test_fresh_consultations_get_distinct_threads():
    h = make_runtime()
    register(h.runtime, "sub", make_build(EchoModel))
    first = await h.runtime.new("sub").invoke([user("one")])
    second = await h.runtime.new("sub").invoke([user("two")])
    assert first.thread_id != second.thread_id
    assert first.response.content == "echo:one|seen:1"
    assert second.response.content == "echo:two|seen:1"
