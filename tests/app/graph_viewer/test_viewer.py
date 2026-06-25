"""GraphViewerModel — the chat-area consumer.

The VM is tested against ``FakeChatArea``, a minimal stand-in exposing exactly the surface the VM
consumes (``OnTopologyChanged`` plus the feed/cursor/rename groups), so the fake doubles as executable
documentation of the contract the VM assumes. Quick-nav's scroll is delivered straight to the
destination feed *entry* via the ``request_scroll_visible`` seam, so it needs nothing on the chat area
itself — the tests use small spies as feed entries.
"""

from rhizome.app.chat_area.branch import BranchPointModel
from rhizome.app.chat_area.conversation_graph import (
    ConversationGraph,
    ConversationItem,
    ConversationNode,
    Cursor,
)
from rhizome.app.graph_viewer import GraphViewerModel
from rhizome.app.model import ViewModelBase

from tests.agent.fakes import build_runtime, EchoModel


class FakeChatArea(ViewModelBase):
    """The slice of ChatAreaModel that GraphViewerModel depends on."""

    class Callbacks(ViewModelBase.Callbacks):
        OnCursorMoved     = "OnCursorMoved"
        OnFeedAppended    = "OnFeedAppended"
        OnFeedRemoved     = "OnFeedRemoved"
        OnFeedCleared     = "OnFeedCleared"
        OnNodeRenamed     = "OnNodeRenamed"
        OnTopologyChanged = "OnTopologyChanged"   # NEW seam

    def __init__(self, graph: ConversationGraph) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnCursorMoved:     Cursor,
            self.Callbacks.OnFeedAppended:    (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedRemoved:     (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedCleared:     ConversationNode,
            self.Callbacks.OnNodeRenamed:     ConversationNode,
            self.Callbacks.OnTopologyChanged: None,
        })
        self.conversation_graph = graph
        self.cursor = graph.root_cursor()
        self.set_cursor_calls: list[int] = []

    def set_cursor(self, target) -> None:
        self.cursor = self.conversation_graph.cursor(target)
        self.set_cursor_calls.append(self.cursor.node.id)
        self.emit(self.Callbacks.OnCursorMoved, self.cursor)


def make_graph() -> ConversationGraph[str]:
    graph = ConversationGraph(build_runtime(lambda: EchoModel()))
    graph.make_root()
    return graph


class DataChanges:
    """Strongly-held counter of OnDataChanged emits (callbacks are weakref'd)."""

    def __init__(self, vm: GraphViewerModel) -> None:
        self.count = 0
        vm.subscribe(vm.Callbacks.OnDataChanged, self._on)

    def _on(self) -> None:
        self.count += 1


class ScrollSpy:
    """A feed entry that records ``request_scroll_visible`` (duck-typed; real feed entries are
    ``ViewModelBase``s that inherit it and emit to their own widget)."""

    def __init__(self) -> None:
        self.scroll_top: bool | None = None

    def request_scroll_visible(self, top: bool = True) -> None:
        self.scroll_top = top


class ScrollRecorder:
    """Strongly-held recorder for a real entry's RequestScrollVisible emits (callbacks are weakref'd)."""

    def __init__(self) -> None:
        self.tops: list[bool] = []

    def record(self, top: bool) -> None:
        self.tops.append(top)


async def test_reflects_the_initial_graph():
    graph = make_graph()
    vm = GraphViewerModel(FakeChatArea(graph))
    assert [d.id for d in vm.display_nodes] == [("node", graph.root.id)]


async def test_topology_change_rebuilds_the_display():
    graph = make_graph()
    chat = FakeChatArea(graph)
    vm = GraphViewerModel(chat)
    changes = DataChanges(vm)

    a = (await graph.branch(graph.root)).node
    b = (await graph.branch(graph.root)).node
    chat.emit(chat.Callbacks.OnTopologyChanged)      # the signal the real graph fires on branch/merge

    assert changes.count == 1
    ids = {d.id for d in vm.display_nodes}
    assert ("branch", graph.root.id) in ids
    assert {("node", a.id), ("node", b.id)} <= ids


async def test_cursor_move_remarks_the_current_node():
    graph = make_graph()
    child = (await graph.branch(graph.root)).node
    chat = FakeChatArea(graph)
    vm = GraphViewerModel(chat)

    chat.set_cursor(child)                            # emits OnCursorMoved → VM rebuilds
    index = {d.id: d for d in vm.display_nodes}
    assert index[("node", child.id)].is_current
    assert not index[("node", graph.root.id)].is_current


async def test_quick_nav_checks_out_node_and_scrolls_to_feed_top():
    graph = make_graph()
    spy = ScrollSpy()
    graph.append(graph.root, spy)                    # the first feed item = the scroll target
    await graph.branch(graph.root)
    chat = FakeChatArea(graph)
    vm = GraphViewerModel(chat)

    vm.quick_nav(("node", graph.root.id))
    assert chat.set_cursor_calls[-1] == graph.root.id
    assert spy.scroll_top is True                    # top-of-feed entry, asked to align to the top


async def test_quick_nav_branch_point_targets_the_indicator():
    graph = make_graph()
    root = graph.root
    chat = FakeChatArea(graph)
    # Mirror ChatAreaModel.branch ordering: the indicator lands while the node is still live, then it forks.
    bp = BranchPointModel(chat, root)
    graph.append(root, bp)
    await graph.branch(root)
    await graph.branch(root)
    vm = GraphViewerModel(chat)

    rec = ScrollRecorder()
    bp.subscribe(bp.Callbacks.RequestScrollVisible, rec.record)   # strongly held — callbacks are weakref'd

    vm.quick_nav(("branch", root.id))
    assert chat.set_cursor_calls[-1] == root.id
    assert rec.tops == [True]                         # the fork indicator scrolled itself to the top


async def test_quick_nav_ignores_a_stale_id():
    graph = make_graph()
    chat = FakeChatArea(graph)
    vm = GraphViewerModel(chat)

    vm.quick_nav(("node", 9999))                      # a selection that no longer exists
    assert chat.set_cursor_calls == []


async def test_display_path_interleaves_branch_points():
    graph = make_graph()
    root = graph.root
    a = (await graph.branch(root)).node
    await graph.branch(root)                          # root forks
    chat = FakeChatArea(graph)
    chat.cursor = graph.cursor(a)
    vm = GraphViewerModel(chat)

    # root forked and is mid-path → its branch point is interleaved before the child.
    assert vm.display_path_for_chat_cursor() == (
        ("node", root.id), ("branch", root.id), ("node", a.id),
    )


async def test_set_mode_is_equality_guarded():
    graph = make_graph()
    vm = GraphViewerModel(FakeChatArea(graph))
    changes = DataChanges(vm)
    vm.set_mode(vm.mode)                              # no change
    assert changes.count == 0
