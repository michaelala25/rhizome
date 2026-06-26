"""GraphViewer view: a headless mount smoke test.

The projection + VM logic is covered in ``tests/app/graph_viewer``; this pins the view wiring with a
minimal ``run_test`` harness over a real ``ChatAreaModel`` — mount the panel, confirm the merge-tree
widget paints the projected graph, the highlight lands on the chat's node, a topology change repaints,
and a node selection routes back to ``quick_nav``.
"""

from textual.app import App, ComposeResult
from textual.widgets import Static

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.app.graph_viewer import GraphViewerModel, Mode
from rhizome.tui.types import Role
from rhizome.tui.widgets.graph_viewer import GraphViewer
from rhizome.tui.widgets.shared.merge_tree import MergeTree as MergeTreeWidget

from tests.agent.fakes import build_runtime, EchoModel


def make_chat() -> ChatAreaModel:
    return ChatAreaModel(build_runtime(lambda: EchoModel()))


def _agent_msg(text: str) -> AgentMessageModel:
    msg = AgentMessageModel()
    msg.body = text
    return msg


class _App(App):
    def __init__(self, vm: GraphViewerModel) -> None:
        super().__init__()
        self._vm = vm

    def compose(self) -> ComposeResult:
        yield GraphViewer(self._vm)


async def test_mounts_and_paints_the_root():
    chat = make_chat()
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        assert [n.id for n in tree._graph_nodes] == [("node", chat.conversation_graph.root.id)]


async def test_highlight_starts_on_the_chat_node():
    chat = make_chat()
    await chat.branch()                          # chat cursor now sits on the new branch leaf
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        assert tree.cursor[-1] == ("node", chat.cursor.node.id)


async def test_topology_change_repaints_the_widget():
    chat = make_chat()
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        await chat.branch()                      # fork → OnTopologyChanged → VM rebuild → set_graph
        await pilot.pause()
        ids = {n.id for n in tree._graph_nodes}
        assert ("branch", chat.conversation_graph.root.id) in ids


async def test_node_selection_routes_to_quick_nav():
    chat = make_chat()
    vm = GraphViewerModel(chat)
    routed: list = []
    vm.quick_nav = lambda display_id: routed.append(display_id)   # spy the routing target
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        tree.post_message(MergeTreeWidget.NodeSelected(tree.cursor, tree.cursor[-1]))
        await pilot.pause()
        assert routed == [tree.cursor[-1]]


async def test_branch_point_paints_a_red_diamond():
    chat = make_chat()
    await chat.branch()                          # fork at the root → a branch-point display node
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        branch = next(n for n in tree._graph_nodes if n.id[0] == "branch")
        assert (branch.marker, branch.style) == ("◆", "red")


async def test_long_label_truncates_in_graph_but_shows_full_in_preview_box():
    chat = make_chat()
    await chat.branch()
    node = chat.cursor.node
    chat.conversation_graph.rename(node, "experiment-auth-v2")     # 18 chars > MAX_LABEL
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        gnode = next(n for n in tree._graph_nodes if n.id == ("node", node.id))
        assert gnode.label == "experiment-auth…"                   # clipped to MAX_LABEL with an ellipsis
        assert len(gnode.label) == GraphViewer.MAX_LABEL

        # Moving the cursor onto the node recovers its full name in the preview box below the diagram.
        display_id = ("node", node.id)
        tree.post_message(MergeTreeWidget.CursorMoved((display_id,), display_id, None))
        await pilot.pause()
        preview = pilot.app.query_one("#gv-preview-text", Static)
        assert "experiment-auth-v2" in preview.render().plain


async def test_preview_box_compacts_long_text_to_head_and_tail():
    chat = make_chat()
    head, mid, tail = "H" * 80, "M" * 200, "T" * 80
    chat.append_item(ChatMessageModel(Role.USER, "go"))
    chat.append_item(_agent_msg(head + mid + tail))      # a long agent run
    vm = GraphViewerModel(chat)
    vm.set_mode(Mode.EXPANDED)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        run_id = next(n.id for n in tree._graph_nodes if n.id[0] == "run")
        tree.post_message(MergeTreeWidget.CursorMoved((run_id,), run_id, None))
        await pilot.pause()

        text = pilot.app.query_one("#gv-preview-text", Static).render().plain
        n = GraphViewer.PREVIEW_CHARS
        assert "…" in text
        assert "H" * n in text and "T" * n in text       # head and tail kept …
        assert "M" not in text                           # … the middle dropped


async def test_ctrl_e_toggles_between_collapsed_and_expanded():
    chat = make_chat()
    chat.append_item(ChatMessageModel(Role.USER, "hi"))    # content so expanded differs from collapsed
    chat.append_item(_agent_msg("hello"))
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        assert all(n.id[0] == "node" for n in tree._graph_nodes)    # collapsed: one node per conv node

        await pilot.press("ctrl+e")
        await pilot.pause()
        assert any(n.id[0] == "msg" for n in tree._graph_nodes)     # expanded: a user-message chunk

        await pilot.press("ctrl+e")
        await pilot.pause()
        assert all(n.id[0] == "node" for n in tree._graph_nodes)    # back to collapsed


async def test_ctrl_e_reanchors_highlight_on_the_current_chunk():
    # The two modes use disjoint id schemes, so the widget can't recover its cursor across the switch;
    # the view must re-anchor it on the chat's current chunk rather than letting it reset to the root.
    chat = make_chat()
    chat.append_item(ChatMessageModel(Role.USER, "hi"))
    run = chat.append_item(_agent_msg("hello"))            # the run's first (and only) item
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test() as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert tree.cursor[-1] == ("run", run.id)          # highlight on the node's final chunk


async def test_click_in_empty_panel_area_focuses_the_tree():
    # A click anywhere in the panel — not only on the tree glyphs — must land focus on the tree so its
    # cursor keys go live. The scroll wrapper is non-focusable, so a click in its empty area bubbles up
    # to the view, whose on_focus redirects inward. Branch first so the (centered) tree leaves an empty
    # top-left corner to click.
    chat = make_chat()
    await chat.branch()
    vm = GraphViewerModel(chat)
    async with _App(vm).run_test(size=(80, 24)) as pilot:
        tree = pilot.app.query_one(MergeTreeWidget)
        pilot.app.set_focus(None)                # start with focus off the tree
        await pilot.pause()
        await pilot.click("#gv-scroll", offset=(0, 0))   # empty corner, away from the centered tree
        await pilot.pause()
        assert pilot.app.focused is tree
