"""The projection: ConversationGraph topology → display DAG, in both modes.

Topology is built with the low-level ``graph.branch`` / ``graph.merge`` primitives (the projection cares
only about the resulting shape + names, not how the branches were made). The collapsed tests use plain
strings as feed entries (collapsed ignores feed content); the expanded tests use the real message VMs,
since expanded mode classifies them — see the ``user`` / ``agent`` / ``tool`` builders below.
"""

from rhizome.app.chat_area.conversation_graph import ConversationGraph
from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.app.chat_area.messages.tool import ToolMessageModel
from rhizome.app.chat_area.thinking import ThinkingIndicatorModel
from rhizome.app.graph_viewer import DisplayKind, Mode, build_display_nodes
from rhizome.tui.types import Role

from tests.agent.fakes import build_runtime, EchoModel


def make_graph() -> ConversationGraph[str]:
    graph = ConversationGraph(build_runtime(lambda: EchoModel()))
    graph.make_root()
    return graph


def disp_ids(nodes) -> list:
    return [d.id for d in nodes]


def by_id(nodes) -> dict:
    return {d.id: d for d in nodes}


# -- feed-entry builders for the expanded tests ----------------------------------------------------

def user(text: str) -> ChatMessageModel:
    return ChatMessageModel(Role.USER, text)


def agent(text: str = "", *, thinking: bool = False) -> AgentMessageModel:
    msg = AgentMessageModel(thinking=thinking)
    msg.body = text
    return msg


def tool(*names: str) -> ToolMessageModel:
    msg = ToolMessageModel()
    for name in names:
        msg.add_tool_call(name, {})
    return msg


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


async def test_preview_uses_name_then_id_fallback():
    graph = make_graph()
    graph.rename(graph.root, "main")
    child = (await graph.branch(graph.root)).node    # left unnamed

    nodes = build_display_nodes(graph, graph.cursor(child), Mode.COLLAPSED)
    index = by_id(nodes)
    assert index[("node", graph.root.id)].preview == "main"
    assert index[("node", child.id)].preview == f"#{child.id}"


# ==================================================================================================
# EXPANDED MODE
# ==================================================================================================

def expanded(graph, cursor):
    return build_display_nodes(graph, cursor, Mode.EXPANDED)


async def test_expanded_user_then_agent_run():
    graph = make_graph()
    root = graph.root
    u = graph.append(root, user("hello"))
    a1 = graph.append(root, agent("hi there"))
    t1 = graph.append(root, tool("search"))
    a2 = graph.append(root, agent("done"))

    index = by_id(expanded(graph, graph.root_cursor()))

    # One user-message chunk, then one agent-run chunk coalescing a1/t1/a2 (ids in feed order).
    msg = index[("msg", u.id)]
    run = index[("run", a1.id)]
    assert (msg.kind, run.kind) == (DisplayKind.USER_MESSAGE, DisplayKind.AGENT_RUN)
    assert msg.item_ids == (u.id,)
    assert run.item_ids == (a1.id, t1.id, a2.id)
    # Chained by came-before edges: the user message is the root, the run hangs off it.
    assert msg.parent_ids == ()
    assert run.parent_ids == (("msg", u.id),)
    # is_current marks the node's FINAL chunk (the run) — that's where the chat sits.
    assert run.is_current and not msg.is_current


async def test_expanded_multiple_runs_in_one_node_chain_in_order():
    graph = make_graph()
    root = graph.root
    u1 = graph.append(root, user("q1"))
    a1 = graph.append(root, agent("a1"))
    u2 = graph.append(root, user("q2"))
    a2 = graph.append(root, agent("a2"))

    nodes = expanded(graph, graph.root_cursor())
    assert disp_ids(nodes) == [("msg", u1.id), ("run", a1.id), ("msg", u2.id), ("run", a2.id)]

    index = by_id(nodes)
    assert index[("run", a1.id)].parent_ids == (("msg", u1.id),)
    assert index[("msg", u2.id)].parent_ids == (("run", a1.id),)
    assert index[("run", a2.id)].parent_ids == (("msg", u2.id),)


async def test_expanded_transparent_items_neither_chunk_nor_break_a_run():
    graph = make_graph()
    root = graph.root
    graph.append(root, user("go"))
    a1 = graph.append(root, agent("part 1"))
    graph.append(root, ThinkingIndicatorModel())                       # transparent
    graph.append(root, ChatMessageModel(Role.SYSTEM, "Entered learn mode."))   # transparent (not USER)
    a2 = graph.append(root, agent("part 2"))

    nodes = expanded(graph, graph.root_cursor())
    runs = [d for d in nodes if d.kind is DisplayKind.AGENT_RUN]
    # The two agent segments split by transparent items still coalesce into ONE run.
    assert len(runs) == 1
    assert runs[0].item_ids == (a1.id, a2.id)


async def test_expanded_empty_node_falls_back_to_a_conversation_chunk():
    graph = make_graph()
    # The root has no user/agent items (a freshly-rooted graph).
    nodes = expanded(graph, graph.root_cursor())
    assert disp_ids(nodes) == [("node", graph.root.id)]
    assert nodes[0].kind is DisplayKind.CONVERSATION
    assert nodes[0].is_current


async def test_expanded_fork_splices_branch_point_below_the_final_chunk():
    graph = make_graph()
    root = graph.root
    graph.append(root, user("root q"))
    a = graph.append(root, agent("root a"))
    c1 = (await graph.branch(root)).node
    c2 = (await graph.branch(root)).node              # root now forks

    index = by_id(expanded(graph, graph.cursor(c1)))

    bp = index[("branch", root.id)]
    assert bp.kind is DisplayKind.BRANCH_POINT
    # The fork marker hangs off root's FINAL chunk (the agent run), not the user message.
    assert bp.parent_ids == (("run", a.id),)
    # Each child is empty → a conversation-fallback chunk attaching to the branch point.
    assert index[("node", c1.id)].parent_ids == (("branch", root.id),)
    assert index[("node", c2.id)].parent_ids == (("branch", root.id),)


async def test_expanded_merge_child_collects_both_parents_final_chunks():
    graph = make_graph()
    root = graph.root
    a = (await graph.branch(root)).node
    b = (await graph.branch(root)).node
    graph.append(a, user("a q")); aa = graph.append(a, agent("a ans"))
    graph.append(b, user("b q")); ab = graph.append(b, agent("b ans"))
    c = (await graph.merge(a, b)).node               # a fresh child below both a and b

    index = by_id(expanded(graph, graph.cursor(c)))

    # a and b each have a single child (c), so neither forks — c (empty → conversation chunk) hangs off
    # both of their final chunks, which the widget renders as a convergence.
    assert set(index[("node", c.id)].parent_ids) == {("run", aa.id), ("run", ab.id)}


async def test_expanded_run_preview_prefers_the_answer_then_tool_names():
    graph = make_graph()
    root = graph.root
    graph.append(root, user("q"))
    a = graph.append(root, agent("The answer is 42"))
    assert by_id(expanded(graph, graph.root_cursor()))[("run", a.id)].preview == "The answer is 42"

    graph2 = make_graph()
    graph2.append(graph2.root, user("q"))
    t = graph2.append(graph2.root, tool("search", "fetch"))
    assert by_id(expanded(graph2, graph2.root_cursor()))[("run", t.id)].preview == "search, fetch"


async def test_expanded_run_preview_skips_thinking_only_segments():
    graph = make_graph()
    root = graph.root
    graph.append(root, user("q"))
    # A thinking segment carries no answer; with nothing else the run reads as a bare "agent".
    th = graph.append(root, agent("pondering…", thinking=True))
    assert by_id(expanded(graph, graph.root_cursor()))[("run", th.id)].preview == "agent"
