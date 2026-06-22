"""AgentGraph / AgentNode: branch & merge topology and semantics, freezing, busy events, and the
worker-driven run lifecycle — on the real stack (create_agent + middleware + one shared checkpointer),
scripted models. The "many threads, one checkpointer" sanity-preservers.

Pure concurrent-isolation is covered by the session suite (independent threads over the shared
checkpointer); here the branch tests assert the extra property a branch adds — state seeded from the
parent with message ids preserved, then divergence.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rhizome.agent.engine import PromptEngine
from rhizome.agent.graph import AgentGraph

from .fakes import (
    ai_contents,
    asking_tool,
    CollectingStreamContext,
    EchoModel,
    make_build,
    make_runtime,
    register,
    run_turn,
    slow_tool,
    ToolOnceModel,
    user,
)


def _graph(model_factory=EchoModel, *, tools=(), engine_factory=PromptEngine):
    """A graph over a one-agent runtime. ``root`` must be registered before ``make_root`` mints the root
    node (and thus a session). Returns (graph, runtime)."""
    h = make_runtime()
    register(h.runtime, "root", make_build(model_factory, tools=tools, engine_factory=engine_factory))
    graph = AgentGraph(h.runtime)
    graph.make_root()
    return graph, h.runtime


# --------------------------------------------------------------------------- #
# Branch semantics
# --------------------------------------------------------------------------- #

async def test_branch_seeds_parent_state_then_diverges():
    graph, _ = _graph()
    root = graph.root
    await run_turn(graph, root, "r")
    root_ids = [m.id for m in (await root.agent_state)["messages"]]
    assert ai_contents(await root.agent_state) == ["echo:r|seen:1"]

    cursor = await graph.branch(root)
    child = cursor.node
    assert root.frozen and not child.frozen
    assert child.thread_id != root.thread_id
    # Seeded by checkpoint copy with message ids preserved (cache spine / merge-by-union prerequisite).
    assert [m.id for m in (await child.agent_state)["messages"]] == root_ids

    await run_turn(graph, cursor, "c")
    # The child saw inherited 2 messages + its own human = 3, and diverged from the parent.
    assert ai_contents(await child.agent_state)[-1] == "echo:c|seen:3"
    # The frozen parent's thread is untouched.
    assert ai_contents(await root.agent_state) == ["echo:r|seen:1"]
    assert [m.id for m in (await root.agent_state)["messages"]] == root_ids


async def test_frozen_and_busy_rules():
    graph, _ = _graph(lambda: EchoModel(delay=0.3))
    root = graph.root
    await run_turn(graph, root, "r")

    cursor = await graph.branch(root)
    leaf = cursor.node

    # A frozen parent refuses all communication.
    assert root.frozen
    with pytest.raises(RuntimeError):
        graph.send(root, user("nope"))
    with pytest.raises(RuntimeError):
        graph.stream(root, CollectingStreamContext())
    with pytest.raises(RuntimeError):
        await graph.invoke(root)

    # A busy leaf cannot be branched (its state is mid-flight).
    ctx = CollectingStreamContext()
    graph.send(leaf, user("slow"))
    graph.stream(leaf, ctx)
    assert leaf.busy
    with pytest.raises(RuntimeError):
        await graph.branch(cursor)
    await ctx.wait()

    # Branching from an already-frozen node is legal — that is what multiple children are.
    sibling = await graph.branch(root)
    assert sibling.node is not leaf


async def test_busy_events_fire_at_the_worker_pinpoints():
    """OnNodeBusyChanged fires exactly when ``node.busy`` flips; the payload bool and the node's live
    value agree at emit time, for both the start edge and the post-teardown idle edge."""
    graph, _ = _graph()
    root = graph.root

    events = []

    def on_busy(node, busy):                 # a named local (not a bare lambda): CallbackHost holds
        events.append((node, busy, node.busy))   # subscribers weakly, so the reference must outlive subscribe

    graph.subscribe(graph.Callbacks.OnNodeBusyChanged, on_busy)
    await run_turn(graph, root, "x")
    assert events == [(root, True, True), (root, False, False)]


# --------------------------------------------------------------------------- #
# Merge semantics  (EXPERIMENTAL — baseline union-into-spine)
# --------------------------------------------------------------------------- #

async def test_merge_unions_histories_into_spine_first():
    graph, _ = _graph()
    root = graph.root
    await run_turn(graph, root, "r")
    root_ids = [m.id for m in (await root.agent_state)["messages"]]

    b1 = await graph.branch(root)
    b2 = await graph.branch(root)
    await run_turn(graph, b1, "one")
    await run_turn(graph, b2, "two")
    ids1 = [m.id for m in (await b1.node.agent_state)["messages"]]
    ids2 = [m.id for m in (await b2.node.agent_state)["messages"]]

    merged = await graph.merge(b1, b2)
    node = merged.node

    # Union by id: shared prefix once, into's suffix first, from_'s divergent suffix appended.
    expected = ids1 + ids2[len(root_ids):]
    assert [m.id for m in (await node.agent_state)["messages"]] == expected
    assert b1.node.frozen and b2.node.frozen and not node.frozen

    # The merged cursor's lineage is the into-spine; both parents reach the single live node.
    assert list(merged) == [root, b1.node, node]
    assert {p.node for p in graph.leaves()} == {node}

    # And it is a functional conversation: 6 union messages + 1 new human = 7 seen.
    await run_turn(graph, merged, "m")
    assert ai_contents(await node.agent_state)[-1] == "echo:m|seen:7"


async def test_merge_guards_reject_self_and_ancestor():
    graph, _ = _graph()
    root = graph.root
    await run_turn(graph, root, "r")
    b1 = await graph.branch(root)
    await graph.branch(root)

    with pytest.raises(ValueError):
        await graph.merge(b1, b1)        # a node with itself
    with pytest.raises(ValueError):
        await graph.merge(b1, root)      # a node with its ancestor


# --------------------------------------------------------------------------- #
# Worker-driven run lifecycle through the graph
# --------------------------------------------------------------------------- #

async def test_eager_payload_consumed_within_same_run():
    graph, _ = _graph(lambda: EchoModel(delay=0.25))
    root = graph.root

    ctx = CollectingStreamContext()
    graph.send(root, user("first"))
    graph.stream(root, ctx)
    await asyncio.sleep(0.1)                  # first model call is in flight (0.25s delay)
    assert root.busy
    graph.send(root, user("eager"), eager=True)
    await ctx.wait()
    assert ctx.exception is None

    # One stream() produced two turns: the eager payload arrived after the final model call, so the
    # session re-entered and the engine ingested it as part of the same run.
    assert ai_contents(await root.agent_state) == ["echo:first|seen:1", "echo:eager|seen:3"]


async def test_cancel_mid_tool_patches_and_resumes():
    """The orphaned-tool-call story end to end through the graph worker: cancel while the tool sleeps,
    verify the patch lands adjacent to the dangling tool_use, then verify the next turn runs cleanly."""
    graph, _ = _graph(lambda: ToolOnceModel(tool_name="slow_tool"), tools=[slow_tool])
    root = graph.root

    ctx = CollectingStreamContext()
    graph.send(root, user("go"))
    graph.stream(root, ctx)

    async def until_tool_call_checkpointed():
        while not any(
            isinstance(m, AIMessage) and m.tool_calls for m in (await root.agent_state).get("messages", [])
        ):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(until_tool_call_checkpointed(), 5)
    graph.cancel(root)
    await ctx.wait()
    assert ctx.cancelled

    # Post-mortem repair wrote a synthetic ToolMessage adjacent to the dangling tool_use.
    messages = (await root.agent_state)["messages"]
    ai_idx = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls)
    patch = messages[ai_idx + 1]
    assert isinstance(patch, ToolMessage)
    assert patch.tool_call_id == messages[ai_idx].tool_calls[0]["id"]
    assert "cancelled" in patch.content.lower()

    # Next turn completes; the model sees the patch as the tool result, the new human lands after it.
    await run_turn(graph, root, "again")
    final = ai_contents(await root.agent_state)[-1]
    assert final.startswith("after-tool:") and "cancelled" in final.lower()
    messages = (await root.agent_state)["messages"]
    assert isinstance(messages[ai_idx + 1], ToolMessage)
    assert isinstance(messages[ai_idx + 2], HumanMessage)


async def test_interrupt_resume_roundtrip():
    graph, _ = _graph(lambda: ToolOnceModel(tool_name="asking_tool"), tools=[asking_tool])
    root = graph.root

    ctx = CollectingStreamContext(interrupt_response="forty-two")
    graph.send(root, user("ask away"))
    graph.stream(root, ctx)
    await ctx.wait()
    assert ctx.exception is None

    assert ctx.interrupts == ["what say you"]
    assert ai_contents(await root.agent_state)[-1] == "after-tool:user-said:forty-two"


async def test_runtime_rebuild_preserves_thread_state():
    """A rebuild (as a bound-option change triggers) swaps the agent; the shared checkpointer is what
    keeps every thread's history meaningful across the swap."""
    graph, runtime = _graph()
    root = graph.root

    await run_turn(graph, root, "a")
    before = runtime._get_agent("root")

    runtime._invalidate("root")              # drop the build; next use rebuilds (checkpointer is shared)
    after = runtime._get_agent("root")
    assert after is not before

    await run_turn(graph, root, "b")
    assert ai_contents(await root.agent_state) == ["echo:a|seen:1", "echo:b|seen:3"]


# --------------------------------------------------------------------------- #
# Message identity: well-known ids are thread-LOCAL, not checkpointer-global
# --------------------------------------------------------------------------- #

_NOTE_ID = "rhizome-test/global-note"


class NoteEngine(PromptEngine):
    """Maintains a well-known-id note whose content tracks the latest user message — the 'global
    resources message' pattern in miniature: the SAME id in every thread, rewritten by each thread's own
    compile. The thing under test is that this shared id stays thread-local through the one checkpointer."""

    async def compile(self, state, ctx):
        update = await super().compile(state, ctx) or {}
        messages = update.get("messages", [])
        last_human = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if last_human is not None:
            update["messages"] = [HumanMessage(content=f"note:{last_human}", id=_NOTE_ID), *messages]
        return update or None


def _notes(state: dict) -> list[tuple[int, str]]:
    """(index, content) of every message carrying the well-known note id."""
    return [(i, m.content) for i, m in enumerate(state["messages"]) if m.id == _NOTE_ID]


async def test_well_known_id_updates_in_place_without_duplication():
    graph, _ = _graph(EchoModel, engine_factory=NoteEngine)
    root = graph.root

    await run_turn(graph, root, "a")
    assert _notes(await root.agent_state) == [(0, "note:a")]

    # A second turn on the SAME thread rewrites the note: same id, same head position, new content —
    # replaced in place (add_messages keys on id), not re-appended at the tail.
    await run_turn(graph, root, "b")
    state = await root.agent_state
    assert _notes(state) == [(0, "note:b")]
    assert len(state["messages"]) == 5   # note + (human, ai) x 2 turns


async def test_same_id_is_thread_local_across_threads():
    """The same well-known id identifies a DIFFERENT message in each thread's state — proof that thread
    ids are thread-local, not checkpointer-global, even though the children inherited the id from one
    parent seed."""
    graph, _ = _graph(EchoModel, engine_factory=NoteEngine)
    root = graph.root

    await run_turn(graph, root, "a")
    b1 = await graph.branch(root)
    b2 = await graph.branch(root)

    # Both children inherited the parent's note (same well-known id, parent's content).
    assert _notes(await b1.node.agent_state) == [(0, "note:a")]
    assert _notes(await b2.node.agent_state) == [(0, "note:a")]

    # Each child rewrites its own note independently; the sibling and the frozen parent are untouched.
    await run_turn(graph, b1, "x1")
    await run_turn(graph, b2, "x2")
    assert _notes(await b1.node.agent_state) == [(0, "note:x1")]
    assert _notes(await b2.node.agent_state) == [(0, "note:x2")]
    assert _notes(await root.agent_state) == [(0, "note:a")]


# --------------------------------------------------------------------------- #
# Topology snapshot
# --------------------------------------------------------------------------- #

async def test_topology_snapshot_tracks_root_then_branch():
    graph, _ = _graph()
    root = graph.root

    snap = graph._topology.snapshot
    assert set(snap.nodes) == {root.id}
    assert snap.node(root.id).parents == () and not snap.node(root.id).frozen and snap.is_leaf(root.id)

    child = (await graph.branch(graph.root_cursor())).node
    snap = graph._topology.snapshot
    assert set(snap.nodes) == {root.id, child.id}
    # Parent froze and gained the child; the child is the live leaf, parented at the root.
    assert snap.node(root.id).children == (child.id,) and snap.node(root.id).frozen
    assert snap.node(child.id).parents == (root.id,) and snap.is_leaf(child.id)


async def test_topology_snapshot_records_merge_parents():
    graph, _ = _graph()
    c1 = await graph.branch(graph.root_cursor())   # root freezes
    c2 = await graph.branch(graph.root_cursor())   # second child off the (frozen) root
    merged = await graph.merge(c1, c2)

    snap = graph._topology.snapshot
    assert set(snap.node(merged.node.id).parents) == {c1.node.id, c2.node.id}
    assert snap.node(c1.node.id).frozen and snap.node(c2.node.id).frozen
    assert snap.is_leaf(merged.node.id)
