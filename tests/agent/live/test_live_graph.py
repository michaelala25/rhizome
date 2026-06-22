"""Live branch/merge semantics — opt-in via ``--live`` + ``ANTHROPIC_API_KEY``.

SCAFFOLD: written here but NOT run in this environment (no key). Branch and merge are verified
deterministically offline (``tests/agent/test_graph.py``); these confirm the same topology holds
against the REAL API — in particular that a MERGED history (two divergent assistant turns unioned onto one
spine) is wire-valid and the conversation can continue, which only a real call can prove. Assertions are
structural (lengths, ids, frozen flags, "a run completed cleanly"), not about content. Run with::

    uv run pytest tests/agent/live --live
"""

import pytest

from rhizome.agent.graph import AgentGraph

from ..fakes import run_turn
from .harness import build_live_runtime, register_live_agent

pytestmark = pytest.mark.live   # whole module is live-gated


def _live_graph() -> AgentGraph:
    rt = build_live_runtime()
    register_live_agent(rt)        # "root" must be registered before make_root mints the root node
    graph = AgentGraph(rt)
    graph.make_root()
    return graph


async def test_live_branch_inherits_then_diverges():
    graph = _live_graph()
    root = graph.root
    await run_turn(graph, root, "Remember the word ALPHA. Reply in one word.")
    root_ids = [m.id for m in (await root.agent_state)["messages"]]

    cursor = await graph.branch(root)
    child = cursor.node
    # The child inherits the parent's history verbatim — ids preserved by the checkpoint copy.
    assert [m.id for m in (await child.agent_state)["messages"]] == root_ids
    assert child.thread_id != root.thread_id

    await run_turn(graph, cursor, "Now remember BETA too. Reply in one word.")
    # The child advanced beyond the inherited prefix; the frozen parent did not.
    assert len((await child.agent_state)["messages"]) > len(root_ids)
    assert [m.id for m in (await root.agent_state)["messages"]] == root_ids


async def test_live_merge_history_is_wire_valid_and_continues():
    graph = _live_graph()
    root = graph.root
    await run_turn(graph, root, "We are brainstorming. Reply in one short word.")

    b1 = await graph.branch(root)
    b2 = await graph.branch(root)
    await run_turn(graph, b1, "Idea one: apples. One word reply.")
    await run_turn(graph, b2, "Idea two: oranges. One word reply.")

    merged = await graph.merge(b1, b2)
    node = merged.node
    # Union onto the into-spine: at least as long as either parent; both parents frozen, child live.
    assert len((await node.agent_state)["messages"]) >= max(
        len((await b1.node.agent_state)["messages"]), len((await b2.node.agent_state)["messages"])
    )
    assert b1.node.frozen and b2.node.frozen and not node.frozen

    # THE live concern: the real API accepts the merged history (two divergent assistant turns on one
    # spine) and the conversation continues — run_turn asserts the run completed without exception.
    await run_turn(graph, merged, "Summarize both ideas in one word.")
    assert (await node.agent_state)["messages"][-1].__class__.__name__ == "AIMessage"
