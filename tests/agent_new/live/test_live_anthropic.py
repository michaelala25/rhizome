"""Live Anthropic pipeline checks — opt-in via ``--live`` + ``ANTHROPIC_API_KEY``.

SCAFFOLD: written here but NOT run in this environment (no key). These validate the pieces only a real
API can confirm — structured output parses end-to-end, repair makes a torn history wire-valid, concurrent
runs don't collide. Assertions are structural (a response arrives; a payload carries the schema's field),
not about exact content. Run with::

    uv run pytest tests/agent_new/live --live
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from .harness import build_live_runtime, CollectingStreamContext, register_live_agent, user

pytestmark = pytest.mark.live   # the whole module is live-gated


async def test_basic_invoke_roundtrip():
    rt = build_live_runtime()
    register_live_agent(rt)
    result = await rt.new("root").invoke([user("Reply with exactly one word.")])
    assert isinstance(result.response, AIMessage)
    assert result.thread_id


async def test_streaming_yields_message_events():
    rt = build_live_runtime()
    register_live_agent(rt)
    ctx = CollectingStreamContext()
    await rt.new("root").stream(ctx, [user("Say hello in one short sentence.")])
    assert ctx.exception is None and not ctx.cancelled
    assert ctx.chunks                     # real streaming produced message events
    assert ctx.completed.is_set()


class _Answer(BaseModel):
    value: int


async def test_structured_response_parses_end_to_end():
    rt = build_live_runtime()
    register_live_agent(rt, response_format=_Answer)
    result = await rt.new("root").invoke([user("Return the structured answer with value = 7.")])
    # Structural: a structured payload came back and carries the schema's field. The value itself is not
    # asserted — parsing the wire format is the contract under test, not the model's arithmetic.
    parsed = result.structured_response
    assert parsed is not None
    assert ("value" in parsed) if isinstance(parsed, dict) else hasattr(parsed, "value")


async def test_repair_makes_orphaned_tool_use_wire_valid():
    """The whole reason repair exists: Anthropic rejects a ``tool_use`` with no adjacent ``tool_result``.
    Seed an orphaned tool call into the checkpoint (as a cancelled run would leave), then make a real call
    — the engine's compile-time repair pass must patch it so the API accepts the history."""
    from ..fakes import noop_tool

    rt = build_live_runtime()
    register_live_agent(rt, tools=[noop_tool])
    s = rt.new("root")

    acq = s.acquire()
    orphan = AIMessage(content="", tool_calls=[{"name": "noop_tool", "args": {}, "id": "orphan-1"}])
    await acq.agent.aupdate_state(acq.config, {"messages": [orphan]}, as_node="__start__")

    result = await s.invoke([user("continue")])   # would 400 without the repair patch
    assert result.response is not None


async def test_concurrent_requests_are_isolated():
    rt = build_live_runtime()
    register_live_agent(rt)
    s1, s2 = rt.new("root"), rt.new("root")
    r1, r2 = await asyncio.gather(s1.invoke([user("one")]), s2.invoke([user("two")]))
    assert r1.response is not None and r2.response is not None
    assert s1.thread_id != s2.thread_id


@tool
async def wait_tool() -> str:
    """Waits a deterministic moment then returns — a window to post an eager payload mid-tool."""
    await asyncio.sleep(1.0)
    return "waited"


async def test_eager_payload_consumed_after_tool_result():
    """Eager mid-tool delivery against the real API. Posts an eager payload while a tool runs and confirms
    (a) the API ACCEPTS the resulting history — no orphaned tool_use — and (b) the eager message landed
    AFTER the tool_result, then was consumed by the post-tool model call. (Depends on the model choosing to
    call the wait tool, which the prompt directs; it's a live/on-demand test.)"""
    rt = build_live_runtime()
    register_live_agent(rt, tools=[wait_tool])
    s = rt.new("root")

    class EagerDuringWait(CollectingStreamContext):
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
            if saw_tool_use and not self._sent:
                self._sent = True
                self._s.send(user("Also work the word KIWI into your reply."), eager=True)

    ctx = EagerDuringWait(s)
    await s.stream(ctx, [user("Call the wait tool exactly once, then reply in one short sentence.")])
    assert ctx.exception is None        # the real API accepted [tool_use, tool_result, eager_human, ...]

    msgs = (await s.agent_state)["messages"]
    ai_idx = next(i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(msgs[ai_idx + 1], ToolMessage)        # tool_result immediately follows tool_use
    eager_idx = next(
        (i for i, m in enumerate(msgs) if isinstance(m, HumanMessage) and "KIWI" in m.content), None
    )
    assert eager_idx is not None and eager_idx > ai_idx + 1   # eager consumed AFTER the tool_result
