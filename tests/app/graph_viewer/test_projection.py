"""The collapsed-mode projection: ConversationGraph topology → display DAG.

Topology is built with the low-level ``graph.branch`` / ``graph.merge`` primitives (the projection cares
only about the resulting shape + names, not how the branches were made), with plain strings as feed
entries.
"""

import pytest

from rhizome.app.chat_area.conversation_graph import ConversationGraph
from rhizome.app.graph_viewer import DisplayKind, Mode, build_display_nodes

from tests.agent.fakes import build_runtime, EchoModel


def make_graph() -> ConversationGraph[str]:
    graph = ConversationGraph(build_runtime(lambda: EchoModel()))
    graph.make_root()
    return graph


def disp_ids(nodes) -> list:
    return [d.id for d in nodes]


def by_id(nodes) -> dict:
    return {d.id: d for d in nodes}


async def test_single_root():
    graph = make_graph()
    nodes = build_display_nodes(graph, graph.root_cursor(), Mode.COLLAPSED)

    assert disp_ids(nodes) == [("node", graph.root.id)]
    root = nodes[0]
    assert root.kind is DisplayKind.CONVERSATION
    assert root.parent_ids == ()
    assert root.node_id == graph.root.id
    assert root.is_current   # the cursor sits at the root


async def test_single_child_is_not_a_fork():
    graph = make_graph()
    child = (await graph.branch(graph.root)).node    # root now has exactly one child

    nodes = build_display_nodes(graph, graph.cursor(child), Mode.COLLAPSED)
    index = by_id(nodes)

    # No branch-point node for a non-fork; the child attaches straight to the root.
    assert set(index) == {("node", graph.root.id), ("node", child.id)}
    assert index[("node", child.id)].parent_ids == (("node", graph.root.id),)
    # is_current tracks the cursor's leaf.
    assert index[("node", child.id)].is_current
    assert not index[("node", graph.root.id)].is_current


async def test_fork_inserts_a_branch_point():
    graph = make_graph()
    root = graph.root
    a = (await graph.branch(root)).node
    b = (await graph.branch(root)).node              # root now has two children → a fork

    nodes = build_display_nodes(graph, graph.cursor(a), Mode.COLLAPSED)
    index = by_id(nodes)

    bp = index[("branch", root.id)]
    assert bp.kind is DisplayKind.BRANCH_POINT
    assert bp.parent_ids == (("node", root.id),)
    assert bp.node_id == root.id
    # Both children hang off the branch point, not the root directly.
    assert index[("node", a.id)].parent_ids == (("branch", root.id),)
    assert index[("node", b.id)].parent_ids == (("branch", root.id),)
    # Sibling order follows creation order (a before b).
    assert disp_ids(nodes).index(("node", a.id)) < disp_ids(nodes).index(("node", b.id))


async def test_merge_child_appears_once_with_both_parents():
    graph = make_graph()
    root = graph.root
    a = (await graph.branch(root)).node
    b = (await graph.branch(root)).node
    c = (await graph.merge(a, b)).node               # a fresh child below both a and b

    nodes = build_display_nodes(graph, graph.cursor(c), Mode.COLLAPSED)

    occurrences = [d for d in nodes if d.id == ("node", c.id)]
    assert len(occurrences) == 1
    # a and b each have a single child (c), so neither gets a branch point — c keeps both directly.
    assert set(occurrences[0].parent_ids) == {("node", a.id), ("node", b.id)}


async def test_label_uses_name_then_id_fallback():
    graph = make_graph()
    graph.rename(graph.root, "main")
    child = (await graph.branch(graph.root)).node    # left unnamed

    nodes = build_display_nodes(graph, graph.cursor(child), Mode.COLLAPSED)
    index = by_id(nodes)
    assert index[("node", graph.root.id)].label == "main"
    assert index[("node", child.id)].label == f"#{child.id}"


async def test_expanded_mode_is_deferred():
    graph = make_graph()
    with pytest.raises(NotImplementedError):
        build_display_nodes(graph, graph.root_cursor(), Mode.EXPANDED)
