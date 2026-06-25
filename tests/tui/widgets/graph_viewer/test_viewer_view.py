"""GraphViewer view: a headless mount smoke test.

The projection + VM logic is covered in ``tests/app/graph_viewer``; this pins the view wiring with a
minimal ``run_test`` harness over a real ``ChatAreaModel`` — mount the panel, confirm the merge-tree
widget paints the projected graph, the highlight lands on the chat's node, a topology change repaints,
and a node selection routes back to ``quick_nav``.
"""

from textual.app import App, ComposeResult
from textual.widgets import Static

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.graph_viewer import GraphViewerModel
from rhizome.tui.widgets.graph_viewer import GraphViewer
from rhizome.tui.widgets.shared.merge_tree import MergeTree as MergeTreeWidget

from tests.agent.fakes import build_runtime, EchoModel


def make_chat() -> ChatAreaModel:
    return ChatAreaModel(build_runtime(lambda: EchoModel()))


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


async def test_long_label_truncates_in_graph_but_shows_full_on_current_line():
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

        # Moving the cursor onto the node recovers its full name on the current-node line.
        display_id = ("node", node.id)
        tree.post_message(MergeTreeWidget.CursorMoved((display_id,), display_id, None))
        await pilot.pause()
        current = pilot.app.query_one("#gv-current", Static)
        assert "experiment-auth-v2" in current.render().plain


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
