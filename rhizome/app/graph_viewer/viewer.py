"""``GraphViewerModel`` — the view-model behind the conversation-graph viewer panel.

It is a *consumer* of the chat area: it owns no conversation state of its own, it reflects the
``ConversationGraph`` the chat area owns. It holds the view-agnostic state the viewer needs — the
projection :class:`Mode` and the current display DAG — subscribes to the chat area's model-level events
to keep that DAG fresh, and turns a node selection into a quick-nav on the chat area.

Why the mode lives here (and the cursor does not)
-------------------------------------------------
The collapsed/expanded mode selects *which set of display nodes exists* — it parameterises the
projection, so it is business state and lives on this VM. The highlight cursor, by contrast, is the
merge-tree widget's own concern (view-side, as elsewhere in the app): this VM stays cursorless and only
answers the pure queries the view needs — :meth:`display_path_for_chat_cursor` (so the view can point the
widget at where the chat currently is) and, later, a preview-by-id.

ChatAreaModel contract
----------------------
This VM is wired to the chat-area VM by the workspace and depends on the following surface:

    reads     ``chat_area.conversation_graph``     the ConversationGraph to project
              ``chat_area.cursor``                 the checked-out path (its leaf = the current node)
    calls     ``chat_area.set_cursor(node|cursor)``  quick-nav: check out a node
    callbacks ``OnCursorMoved``                    re-mark the "you are here" node
              ``OnNodeRenamed``                    a branch label changed
              ``OnTopologyChanged``                a branch/merge changed the node set
              ``OnFeedAppended`` / ``OnFeedRemoved`` / ``OnFeedCleared``   (subscribed for expanded mode)

Quick-nav scrolls the chat to the target by calling ``request_scroll_visible`` on the destination feed
*entry* — a generic ``ViewModelBase`` seam, so the entry's own widget scrolls itself into view; no
bespoke chat-area method is needed.

Emits ``OnDataChanged`` whenever the display DAG is rebuilt; the view repaints by pushing the fresh nodes
into its merge-tree widget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Hashable

from rhizome.app.chat_area.branch import BranchPointModel
from rhizome.app.model import ViewModelBase

from .projection import DisplayKind, DisplayNode, Mode, build_display_nodes

if TYPE_CHECKING:
    from rhizome.app.chat_area.chat_area import ChatAreaModel
    from rhizome.app.chat_area.conversation_graph import ConversationNode


class GraphViewerModel(ViewModelBase):
    """Reflects the chat area's ``ConversationGraph`` as a display DAG; see module docstring."""

    class Callbacks(ViewModelBase.Callbacks):
        OnDataChanged = "OnDataChanged"   # display DAG rebuilt — the view re-pushes it into the widget

    def __init__(self, chat_area: ChatAreaModel) -> None:
        super().__init__()
        self.make_callback_groups({self.Callbacks.OnDataChanged: None})

        self._chat = chat_area
        self._mode = Mode.COLLAPSED
        self._display: list[DisplayNode] = []
        self._index: dict[Hashable, DisplayNode] = {}

        # Reflect the chat area: every event that can change the projected graph (or which node is
        # current) triggers a rebuild. Bound-method subscribers are weakly held; this VM and the chat
        # area share the workspace's lifetime, so the subscriptions never dangle.
        cb = chat_area.Callbacks
        chat_area.subscribe(cb.OnTopologyChanged, self._on_topology_changed)
        chat_area.subscribe(cb.OnCursorMoved, self._on_cursor_moved)
        chat_area.subscribe(cb.OnNodeRenamed, self._on_node_renamed)
        chat_area.subscribe(cb.OnFeedAppended, self._on_feed_changed)
        chat_area.subscribe(cb.OnFeedRemoved, self._on_feed_changed)
        chat_area.subscribe(cb.OnFeedCleared, self._on_feed_cleared)

        self._rebuild()

    # ------------------------------------------------------------------
    # Read-only accessors (view-side)
    # ------------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def display_nodes(self) -> list[DisplayNode]:
        """The current display DAG, in a layout-friendly order (root first, children in creation order)."""
        return self._display

    def display_node(self, display_id: Hashable) -> DisplayNode | None:
        return self._index.get(display_id)

    def preview_for(self, display_id: Hashable) -> str:
        """The full (untruncated) source text for a display node — what the view compacts into the preview
        box, so text the diagram chip had to clip is still glimpsable. A branch-point node carries no text
        of its own, so it borrows the name of the conversation node it forks from."""
        dn = self._index.get(display_id)
        if dn is None:
            return ""
        if dn.preview:
            return dn.preview
        node = self._chat.conversation_graph.node(dn.node_id)
        return node.name or f"#{node.id}"

    def display_path_for_chat_cursor(self) -> tuple[Hashable, ...]:
        """The widget id-path that mirrors the chat's checked-out path — so the view can land the
        highlight on "where the chat is" (its tip = the chat's current node, or, in expanded mode, that
        node's final chunk). Branch-point ids are interleaved wherever the path crosses a fork, matching
        the edges the projection builds.

        Mode-agnostic: the per-node chunk ids are pulled from the freshly-built display, so this is one
        chunk per node in collapsed mode and the user/agent-run chain in expanded mode without this method
        having to know which is live."""
        graph = self._chat.conversation_graph
        nodes = self._chat.cursor.nodes()

        chunks_by_node: dict[int, list[Hashable]] = {}
        for d in self._display:
            if d.kind is not DisplayKind.BRANCH_POINT:
                chunks_by_node.setdefault(d.node_id, []).append(d.id)

        out: list[Hashable] = []
        for i, node in enumerate(nodes):
            out.extend(chunks_by_node.get(node.id, (("node", node.id),)))
            is_last = i == len(nodes) - 1
            if not is_last and len(graph.children(node)) >= 2:
                out.append(("branch", node.id))
        return tuple(out)

    # ------------------------------------------------------------------
    # Mutators / actions
    # ------------------------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        """Switch projection. Equality-guarded; rebuilds and emits ``OnDataChanged`` on a real change."""
        if mode == self._mode:
            return
        self._mode = mode
        self._rebuild()

    def quick_nav(self, display_id: Hashable) -> None:
        """Check the chat out at the conversation node behind ``display_id`` and scroll its feed into
        view. A conversation node lands at the top of its feed; a branch-point node lands on the fork
        indicator itself. No-op for an unknown id (a stale selection after a rebuild)."""
        dn = self._index.get(display_id)
        if dn is None:
            return
        node = self._chat.conversation_graph.node(dn.node_id)
        # TODO(merge): a node behind a merge has several lineages; the widget hands us the exact path it
        # was reached by, so a merge-correct nav would rebuild the cursor from that path rather than
        # letting set_cursor pick an arbitrary one. Fine until merges are exercised.
        self._chat.set_cursor(node)
        target = self._scroll_target_entry(dn, node)
        if target is not None:
            target.request_scroll_visible(top=True)

    # ------------------------------------------------------------------
    # Chat-area event handlers (thin — every relevant change is a rebuild)
    # ------------------------------------------------------------------

    def _on_topology_changed(self) -> None:
        self._rebuild()

    def _on_cursor_moved(self, cursor) -> None:
        self._rebuild()

    def _on_node_renamed(self, node: ConversationNode) -> None:
        self._rebuild()

    def _on_feed_changed(self, node: ConversationNode, item) -> None:
        self._rebuild()

    def _on_feed_cleared(self, node: ConversationNode) -> None:
        self._rebuild()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        self._display = build_display_nodes(self._chat.conversation_graph, self._chat.cursor, self._mode)
        self._index = {d.id: d for d in self._display}
        self.emit(self.Callbacks.OnDataChanged)

    def _scroll_target_entry(self, dn: DisplayNode, node: ConversationNode) -> ViewModelBase | None:
        """The feed *entry* ``quick_nav`` scrolls into view: the fork indicator for a branch-point node,
        the first of a chunk's own items in expanded mode, else the first item in the node's feed (its
        top). Each entry is a ``ViewModelBase`` whose view scrolls itself via ``request_scroll_visible``.
        Read live so it tracks late feed appends."""
        if dn.kind is DisplayKind.BRANCH_POINT:
            return next((it.entry for it in node.feed if isinstance(it.entry, BranchPointModel)), None)
        if dn.item_ids:
            first = dn.item_ids[0]
            return next((it.entry for it in node.feed if it.id == first), None)
        return node.feed[0].entry if node.feed else None
