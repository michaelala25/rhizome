"""ConversationGraph: feed identity & events, frozen-feed rules, and last-visited navigation.

Topology/agent-state semantics (branch seeding, freeze rules, thread isolation) are covered by
tests/agent/test_graph.py; here we exercise the conversation layer on top, with plain strings
standing in for feed entries.
"""

import pytest

from rhizome.agent.graph import Cursor
from rhizome.app.chat_area.conversation_graph import ConversationGraph

from tests.agent.fakes import ai_contents, build_runtime, EchoModel, run_turn


def make_graph() -> ConversationGraph[str]:
    graph = ConversationGraph(build_runtime(lambda: EchoModel()))
    graph.make_root()
    return graph


class Events:
    """Strongly-held subscriber (callbacks are weakref'd) recording every graph event."""

    def __init__(self, graph: ConversationGraph[str]) -> None:
        self.appended, self.removed, self.cleared, self.renamed = [], [], [], []
        graph.subscribe(graph.Callbacks.OnFeedAppended, self.on_appended)
        graph.subscribe(graph.Callbacks.OnFeedRemoved, self.on_removed)
        graph.subscribe(graph.Callbacks.OnFeedCleared, self.on_cleared)
        graph.subscribe(graph.Callbacks.OnNodeRenamed, self.on_renamed)

    def on_appended(self, node, item) -> None:
        self.appended.append((node, item))

    def on_removed(self, node, item) -> None:
        self.removed.append((node, item))

    def on_cleared(self, node) -> None:
        self.cleared.append(node)

    def on_renamed(self, node) -> None:
        self.renamed.append(node)


# ------------------------------------------------------------------------------------------------
# Feed
# ------------------------------------------------------------------------------------------------

async def test_feed_ids_are_graph_wide_and_events_carry_node_and_item():
    graph = make_graph()
    events = Events(graph)
    root = graph.root

    a = graph.append(root, "a")
    b = graph.append(root, "b")
    child = (await graph.branch(root)).node
    c = graph.append(child, "c")

    # One monotonic counter across all nodes: ids stay unique no matter which feed holds the item.
    assert (a.id, b.id, c.id) == (0, 1, 2)
    assert (a.entry, c.entry) == ("a", "c")
    assert events.appended == [(root, a), (root, b), (child, c)]


async def test_frozen_feeds_are_sealed_but_removal_is_allowed():
    graph = make_graph()
    root = graph.root
    item = graph.append(root, "kept")
    stale = graph.append(root, "stale")
    await graph.branch(root)  # freezes root

    with pytest.raises(ValueError, match="frozen"):
        graph.append(root, "nope")
    with pytest.raises(ValueError, match="frozen"):
        graph.clear_feed(root)

    # Removal is cleanup, not new history.
    assert graph.remove(root, stale.id) is stale
    assert [i.id for i in root.feed] == [item.id]


def test_remove_by_id_and_clear():
    graph = make_graph()
    events = Events(graph)
    root = graph.root

    a, b, c = (graph.append(root, s) for s in "abc")
    assert graph.remove(root, b.id) is b
    assert [i.id for i in root.feed] == [a.id, c.id]

    # Missing id: no removal, no event.
    assert graph.remove(root, 999) is None
    assert events.removed == [(root, b)]

    graph.clear_feed(root)
    assert root.feed == [] and events.cleared == [root]
    graph.clear_feed(root)  # already empty: no second event
    assert events.cleared == [root]


async def test_visible_feed_concatenates_along_the_cursor_path():
    graph = make_graph()
    root = graph.root
    r0, r1 = graph.append(root, "r0"), graph.append(root, "r1")

    b1 = await graph.branch(root)
    b2 = await graph.branch(root)
    c1 = graph.append(b1, "c1")
    c2 = graph.append(b2, "c2")

    # Each sibling sees the shared prefix plus only its own suffix.
    assert graph.visible_feed(b1) == [r0, r1, c1]
    assert graph.visible_feed(b2) == [r0, r1, c2]
    assert graph.feed_segments(b1) == [(root, [r0, r1]), (b1.node, [c1])]

    # Segments are snapshots: mutating one must not touch the node's real feed.
    graph.feed_segments(b1)[1][1].append("rogue")
    assert b1.node.feed == [c1]


def test_rename_emits_with_equality_guard():
    graph = make_graph()
    events = Events(graph)
    root = graph.root

    graph.rename(root, "main")
    graph.rename(root, "main")  # no change, no event
    assert root.name == "main" and events.renamed == [root]

    graph.rename(root, None)
    assert root.name is None and events.renamed == [root, root]


# ------------------------------------------------------------------------------------------------
# Conversation state vs agent state across branch
# ------------------------------------------------------------------------------------------------

async def test_branch_inherits_agent_state_but_not_conversation_state():
    graph = make_graph()
    root = graph.root
    await run_turn(graph, root, "hello")

    graph.append(root, "feed-item")
    graph.rename(root, "main")
    root.pending_interrupt = "blocked"

    child = (await graph.branch(root)).node

    # The agent thread was seeded from the parent; the conversation bookkeeping starts fresh.
    assert ai_contents(await child.agent_state) == ["echo:hello|seen:1"]
    assert child.feed == [] and child.name is None
    assert child.pending_interrupt is None and child.last_visited_child is None


async def test_make_node_wires_topology_and_node_id_into_context():
    graph = make_graph()
    root = graph.root
    root_ctx = root.session.agent_context
    # The one shared topology cell, plus this node's own id, reach the session's compile context.
    assert root_ctx.topology is graph._topology and root_ctx.node_id == root.id

    child = (await graph.branch(root)).node
    child_ctx = child.session.agent_context
    assert child_ctx.topology is graph._topology       # same cell every node pulls from
    assert child_ctx.node_id == child.id != root_ctx.node_id


async def test_make_node_wires_app_state_and_branch_inherits_mode():
    graph = make_graph()
    root = graph.root

    # The node's app-settings store is the SAME object the session compiles against (a live channel,
    # never copied for the node's life), defaulting to idle.
    assert root.app_state is root.session.agent_context.app_state
    assert root.app_state.mode == "idle"

    # A branch taken after a mode switch inherits the parent's mode through derive, then diverges.
    root.app_state.set_mode("learn")
    child = (await graph.branch(root)).node
    assert child.app_state is not root.app_state       # distinct per-node store
    assert child.app_state.mode == "learn"             # seeded from the parent on branch
    child.app_state.set_mode("review")
    assert root.app_state.mode == "learn"              # divergent afterward


# ------------------------------------------------------------------------------------------------
# Navigation
# ------------------------------------------------------------------------------------------------

async def build_fork() -> tuple[ConversationGraph[str], Cursor, Cursor]:
    """Root with two children B and C, where B has a grandchild E. Returns (graph, e_cursor,
    c_cursor) with the E-side path recorded as visited."""
    graph = make_graph()
    b = await graph.branch(graph.root)
    e = await graph.branch(b)
    c = await graph.branch(graph.root)
    graph.record_visit(e)
    return graph, e, c


async def test_swap_sibling_away_and_back_restores_the_deep_path():
    graph, e, _c = await build_fork()
    root, b_node, e_node = e.nodes()

    # Swap at the root's branch point: descended child B -> sibling C (creation order).
    at_c = graph.swap_sibling(e, +1, at=root)
    assert at_c.nodes() == (root, _c.node)

    # Swap back: B deepens through its last-visited memory all the way down to E.
    back = graph.swap_sibling(at_c, -1)
    assert back.nodes() == (root, b_node, e_node)


async def test_descend_deepens_through_memory_and_ascend_does_not():
    graph, e, _c = await build_fork()
    root, b_node, e_node = e.nodes()

    # Ascend truncates exactly as asked — no re-deepening past the new leaf.
    at_b = graph.ascend(e)
    assert at_b.nodes() == (root, b_node)
    at_root = graph.ascend(e, to=root)
    assert at_root.nodes() == (root,)

    # Descending from the root restores the full previously-visited chain.
    assert graph.descend(at_root, b_node).nodes() == (root, b_node, e_node)


async def test_navigation_error_cases():
    graph, e, c = await build_fork()
    root = graph.root

    with pytest.raises(ValueError, match="at the root"):
        graph.ascend(graph.root_cursor())
    with pytest.raises(ValueError, match="proper ancestor"):
        graph.ascend(e, to=c.node)          # not on the path
    with pytest.raises(ValueError, match="proper ancestor"):
        graph.ascend(e, to=e.node)          # the leaf is not a *proper* ancestor

    with pytest.raises(ValueError, match="direction"):
        graph.swap_sibling(e, 0)
    with pytest.raises(ValueError, match="no siblings"):
        graph.swap_sibling(graph.root_cursor(), +1)
    with pytest.raises(ValueError, match="no sibling in that direction"):
        graph.swap_sibling(c, +1)           # C is the rightmost child of the root
    with pytest.raises(ValueError, match="no sibling in that direction"):
        graph.swap_sibling(e, -1)           # E is B's only child (leaf-parent default)


async def test_deepen_ignores_memory_that_is_no_longer_a_child():
    graph, e, c = await build_fork()
    root = graph.root

    # Corrupt the memory on purpose: point the root at a node that isn't its child.
    root.last_visited_child = e.node
    assert graph.deepen(graph.root_cursor()).nodes() == (root,)
