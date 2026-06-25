"""``GraphViewer`` — the view for the conversation-graph viewer panel.

A vertical panel that paints the ``GraphViewerModel``'s display DAG with the shared ``MergeTree`` widget
and routes its selections back to the chat area::

    [section header: Conversation Graph]
    [merge-tree diagram]          — the branch topology (1fr, centered, scrolls if it overflows)
    [current-node line]           — the full name of the node the cursor sits on
    [key hint]

The ``MergeTree`` widget owns the highlight cursor (a root→node path through the diagram); this view
only adapts the VM's semantic ``DisplayNode``s into the widget's ``GraphNode``s — mapping ``kind`` /
``is_current`` to a marker + style and clipping over-long labels to ``MAX_LABEL`` (presentation is the
view's call) — and pushes them in on every ``OnDataChanged``. Because the diagram truncates, the
current-node line below it always shows the cursor node's *full* name. A ``NodeSelected`` quick-navs the
chat there; the highlight starts on the chat's current node, and focus delegates inward to the tree so
its cursor keys are live.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import ScrollableContainer
from textual.events import Focus
from textual.geometry import Spacing
from textual.widgets import Static

from rhizome.app.graph_viewer import DisplayKind, DisplayNode, GraphViewerModel
from rhizome.tui.widgets.shared.merge_tree import GraphNode, MergeTree as MergeTreeWidget
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableViewBase

_DESC = "Navigate branches. enter jumps the conversation to a node."


class GraphViewer(NavigableViewBase[GraphViewerModel]):
    """Root view for the conversation-graph viewer panel. See module docstring."""

    DEFAULT_CSS = """
    GraphViewer {
        layout: vertical;
        background: $surface-darken-1;
        width: 1fr;
        height: 1fr;
        padding: 0 1 1 1;
    }
    GraphViewer .gv-section-header {
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }
    GraphViewer #gv-scroll {
        height: 1fr;
        background: transparent;
        overflow: auto auto;
        align: center middle;
    }
    GraphViewer #gv-current {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
    }
    GraphViewer #gv-keyhint {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    # Long names are truncated to this width (with an ellipsis) before they reach the diagram, so a wide
    # fork stays readable; the full name is always shown on the current-node line below the graph.
    MAX_LABEL = 16

    def __init__(self, vm: GraphViewerModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._tree: MergeTreeWidget | None = None
        self._scroll: ScrollableContainer | None = None
        self._current: Static | None = None
        # OnDataChanged is the structural channel (topology / rename / cursor move): the view re-pushes
        # the whole graph and the widget recovers its own cursor by id.
        self._vm.subscribe(self._vm.Callbacks.OnDataChanged, self._on_data_changed)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        yield Static(self._header(), classes="gv-section-header")
        self._tree = MergeTreeWidget(self._to_nodes(), id="gv-tree")
        # ``can_focus=False``: the scroll wrapper must not be a focus target itself, or a click in its
        # empty area would land on it (the nearest focusable ancestor) instead of bubbling up to this
        # view — whose ``on_focus`` redirects inward to the tree. The tree stays the only interactive child.
        self._scroll = ScrollableContainer(id="gv-scroll", can_focus=False)
        with self._scroll:
            yield self._tree
        self._current = Static(id="gv-current")
        yield self._current
        yield Static(self._keyhint(), id="gv-keyhint")

    def on_mount(self) -> None:
        tree = self._tree
        if tree is None:
            return
        # Land the highlight on the chat's current node, then focus the tree so the arrows drive it. The
        # cursor move publishes a ``CursorMoved`` that fills in the current-node line and scrolls it visible.
        tree.set_cursor(self._vm.display_path_for_chat_cursor())
        tree.focus()

    def on_unmount(self) -> None:
        # ``ViewBase.on_unmount`` (the standard unsubscribes) fires too via Textual's MRO dispatch, so
        # we do NOT call ``super()`` — just drop this view's own subscription.
        self._vm.unsubscribe(self._vm.Callbacks.OnDataChanged, self._on_data_changed)

    # ------------------------------------------------------------------
    # Focus — delegate inward to the tree (the only interactive child)
    # ------------------------------------------------------------------

    def on_focus(self, event: Focus) -> None:
        # ``ViewBase.on_focus`` (VM notify) and ``NavigableViewBase.on_focus`` (border) still fire via
        # MRO; this only redirects focus so the tree's cursor keys are live when the panel is focused.
        if self._tree is not None:
            self._tree.focus()

    # ------------------------------------------------------------------
    # VM → widget
    # ------------------------------------------------------------------

    def _on_data_changed(self) -> None:
        if self._tree is not None:
            # set_graph republishes the (recovered) cursor via CursorMoved, which resyncs the current-node
            # line and re-anchors the scroll — so there is nothing to do here beyond pushing the new nodes.
            self._tree.set_graph(self._to_nodes())

    def _to_nodes(self) -> list[GraphNode]:
        return [self._graph_node(d) for d in self._vm.display_nodes]

    def _graph_node(self, d: DisplayNode) -> GraphNode:
        marker, style = self._present(d)
        return GraphNode(d.id, d.parent_ids, self._truncate(d.label), marker=marker, style=style)

    def _truncate(self, label: str) -> str:
        """Clip an over-long label to ``MAX_LABEL`` with a trailing ellipsis (the full name lives on the
        current-node line). Short labels pass through untouched."""
        return label if len(label) <= self.MAX_LABEL else label[: self.MAX_LABEL - 1] + "…"

    def _present(self, d: DisplayNode) -> tuple[str | None, str | None]:
        """Marker + resting style per node. Cursor-path styling is the widget's own concern (it overrides
        the resting style along the highlighted path), so these are the un-highlighted looks."""
        if d.kind is DisplayKind.BRANCH_POINT:
            return ("◆", "red")
        if d.is_current:
            return (None, "bold green")        # "you are here" — keep the ● / ◆ glyph, colour it
        return (None, None)                    # widget defaults: ● node / ◆ merge

    # ------------------------------------------------------------------
    # Widget → VM
    # ------------------------------------------------------------------

    def on_merge_tree_node_selected(self, event: MergeTreeWidget.NodeSelected) -> None:
        self._vm.quick_nav(event.node)

    def on_merge_tree_node_highlighted(self, event: MergeTreeWidget.NodeHighlighted) -> None:
        # A user cursor move. The preview overlay lands here in a later increment; the current-node line and
        # scroll follow ``CursorMoved`` instead (it also covers the programmatic moves this one does not).
        pass

    def on_merge_tree_cursor_moved(self, event: MergeTreeWidget.CursorMoved) -> None:
        # The cursor settled (any cause): name where it is, and scroll the diagram so it stays visible.
        self._set_current(event.node)
        self._scroll_cursor_visible(event.region)

    # ------------------------------------------------------------------
    # Viewport — this view owns the scroll container, so it decides how to reveal the cursor the widget
    # reports. The widget hands us the cell in its own content space; we translate into the container's.
    # ------------------------------------------------------------------

    def _scroll_cursor_visible(self, region) -> None:
        if region is None or self._tree is None or self._scroll is None:
            return
        target = region.translate(self._tree.virtual_region.offset)
        self._scroll.scroll_to_region(target, spacing=Spacing.all(1), animate=False)

    # ------------------------------------------------------------------
    # Current-node line — the full name of where the cursor sits, recovering any label the diagram clipped
    # ------------------------------------------------------------------

    def _set_current(self, display_id) -> None:
        if self._current is not None:
            self._current.update(self._current_text(display_id))

    def _current_text(self, display_id) -> Text:
        name = self._vm.label_for(display_id) if display_id is not None else ""
        text = Text()
        text.append("current  ", style="#707070")
        text.append(name or "—", style="#b0b0b0")
        return text

    # ------------------------------------------------------------------
    # Static content
    # ------------------------------------------------------------------

    def _header(self) -> Text:
        text = Text()
        text.append("Conversation Graph\n", style="bold")
        text.append(_DESC, style="#707070")
        return text

    _KEYHINT_PAIRS = (("↑↓←→", "navigate"), ("enter", "jump"))

    def _keyhint(self) -> Text:
        text = Text()
        for i, (key, action) in enumerate(self._KEYHINT_PAIRS):
            if i:
                text.append("   ")
            text.append(key, style="#a0a0a0")
            text.append(" ")
            text.append(action, style="#707070")
        return text
