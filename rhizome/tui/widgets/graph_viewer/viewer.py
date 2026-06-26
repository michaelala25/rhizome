"""``GraphViewer`` — the view for the conversation-graph viewer panel.

A vertical panel that paints the ``GraphViewerModel``'s display DAG with the shared ``MergeTree`` widget
and routes its selections back to the chat area::

    [section header: Conversation Graph]
    [merge-tree diagram]          — the branch topology (1fr, centered, scrolls if it overflows)
    [preview box]                 — a fixed-height glimpse of the highlighted node's text (head … tail)
    [key hint]

The ``MergeTree`` widget owns the highlight cursor (a root→node path through the diagram); this view
only adapts the VM's semantic ``DisplayNode``s into the widget's ``GraphNode``s — mapping ``kind`` /
``is_current`` to a marker + style and clipping over-long ``preview`` text to ``MAX_LABEL`` for the chip
(presentation is the view's call) — and pushes them in on every ``OnDataChanged``. The diagram chip is a
hard clip, so the preview box below shows the head and tail of the highlighted node's full source text —
a glance, not a render; the real content is one quick-nav away in the chat. Its height is fixed so the
diagram's region never jumps as the text length varies. A ``NodeSelected`` quick-navs the chat there; the
highlight starts on the chat's current node, and focus delegates inward to the tree so its cursor keys are
live. ``ctrl+e`` toggles the VM between the collapsed and expanded projections (the binding lives here,
not on the domain-agnostic widget — see ``action_toggle_mode``).
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.events import Focus
from textual.geometry import Spacing
from textual.widgets import Static

from rhizome.app.graph_viewer import DisplayKind, DisplayNode, GraphViewerModel, Mode
from rhizome.tui.widgets.shared.merge_tree import GraphNode, MergeTree as MergeTreeWidget
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableViewBase

_DESC = "Navigate branches. enter jumps the conversation to a node; ctrl+e expands/collapses."


class GraphViewer(NavigableViewBase[GraphViewerModel]):
    """Root view for the conversation-graph viewer panel. See module docstring."""

    # ``ctrl+e`` toggles the projection mode. The binding lives here, on the domain view, not on the shared
    # ``MergeTree`` (which knows nothing of collapsed/expanded) — the tree holds focus and lets the key
    # bubble up to this view, whose action drives the VM.
    BINDINGS = [Binding("ctrl+e", "toggle_mode", "Expand/collapse", show=False)]

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
    GraphViewer #gv-preview {
        height: 5;                     /* fixed — the graph's 1fr region never jumps as text length varies */
        margin: 1 0;                   /* a blank row above (off the graph) and below (off the keyhint bar) */
        padding: 0 1;
        background: $surface;          /* a faint box, set apart from the panel ground */
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    GraphViewer #gv-preview-text {
        height: auto;
        background: transparent;
    }
    GraphViewer #gv-keyhint {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    # Long previews are truncated to this width (with an ellipsis) before they reach the diagram, so a wide
    # fork stays readable; the head/tail of the full text is glimpsable in the preview box below the graph.
    MAX_LABEL = 16

    # Preview-box compaction: show the first and last ``PREVIEW_CHARS`` of the (often long) source text with
    # an ellipsis between, but only once it is at least ``2*PREVIEW_CHARS + PREVIEW_BUFFER`` long — below that
    # the head and tail would meet, so the whole text is shown instead. It is a glance, not the content; the
    # full message lives one quick-nav away, rendered by its own feed widget.
    PREVIEW_CHARS = 64
    PREVIEW_BUFFER = 32

    def __init__(self, vm: GraphViewerModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._tree: MergeTreeWidget | None = None
        self._scroll: ScrollableContainer | None = None
        self._preview: Static | None = None
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
        # The preview box: a fixed-height scroll region (``can_focus=False`` so it never steals focus from
        # the tree — it scrolls on the wheel) wrapping the text static that tracks the highlighted node.
        self._preview = Static(id="gv-preview-text")
        with ScrollableContainer(id="gv-preview", can_focus=False):
            yield self._preview
        yield Static(self._keyhint(), id="gv-keyhint")

    def on_mount(self) -> None:
        tree = self._tree
        if tree is None:
            return
        # Land the highlight on the chat's current node, then focus the tree so the arrows drive it. The
        # cursor move publishes a ``CursorMoved`` that fills in the preview box and scrolls it visible.
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
    # Mode toggle (ctrl+e)
    # ------------------------------------------------------------------

    def action_toggle_mode(self) -> None:
        """Flip the projection between collapsed and expanded. The toggle (stateless cycling) is the view's
        call; the VM owns the *build* (``set_mode`` rebuilds and republishes). Because the two modes use
        disjoint id schemes the widget cannot recover its cursor across the switch, so we re-anchor it on
        the chat's current chunk — ``set_mode``'s rebuild has already pushed the new nodes by the time it
        returns (the ``OnDataChanged`` dispatch is synchronous)."""
        self._vm.set_mode(Mode.COLLAPSED if self._vm.mode is Mode.EXPANDED else Mode.EXPANDED)
        if self._tree is not None:
            self._tree.set_cursor(self._vm.display_path_for_chat_cursor())

    # ------------------------------------------------------------------
    # VM → widget
    # ------------------------------------------------------------------

    def _on_data_changed(self) -> None:
        if self._tree is not None:
            # set_graph republishes the (recovered) cursor via CursorMoved, which resyncs the preview box
            # and re-anchors the scroll — so there is nothing to do here beyond pushing the new nodes.
            self._tree.set_graph(self._to_nodes())

    def _to_nodes(self) -> list[GraphNode]:
        return [self._graph_node(d) for d in self._vm.display_nodes]

    def _graph_node(self, d: DisplayNode) -> GraphNode:
        marker, style = self._present(d)
        return GraphNode(d.id, d.parent_ids, self._truncate(d.preview), marker=marker, style=style)

    def _truncate(self, text: str) -> str:
        """Clip an over-long preview to ``MAX_LABEL`` with a trailing ellipsis (the head/tail lives in the
        preview box below). Short text passes through untouched."""
        return text if len(text) <= self.MAX_LABEL else text[: self.MAX_LABEL - 1] + "…"

    def _present(self, d: DisplayNode) -> tuple[str | None, str | None]:
        """Marker + resting style per node. Cursor-path styling is the widget's own concern (it overrides
        the resting style along the highlighted path), so these are the un-highlighted looks. ``is_current``
        recolours the node green while keeping its kind's glyph — it is the "you are here" tint the chat's
        position keeps even when the widget cursor has wandered off it."""
        marker, style = self._kind_look(d.kind)
        if d.is_current:
            style = "bold green"
        return (marker, style)

    def _kind_look(self, kind: DisplayKind) -> tuple[str | None, str | None]:
        """Resting marker + style for each kind. ``None`` marker keeps the widget default (● node / ◆
        merge). Expanded mode reads as user prompts (filled, cyan) anchoring lighter agent runs (hollow)."""
        if kind is DisplayKind.BRANCH_POINT:
            return ("◆", "red")
        if kind is DisplayKind.USER_MESSAGE:
            return (None, "cyan")
        if kind is DisplayKind.AGENT_RUN:
            return ("○", "#808080")
        return (None, None)                    # CONVERSATION — widget defaults

    # ------------------------------------------------------------------
    # Widget → VM
    # ------------------------------------------------------------------

    def on_merge_tree_node_selected(self, event: MergeTreeWidget.NodeSelected) -> None:
        self._vm.quick_nav(event.node)

    def on_merge_tree_node_highlighted(self, event: MergeTreeWidget.NodeHighlighted) -> None:
        # A user cursor move. The preview box and scroll follow ``CursorMoved`` instead (it also covers the
        # programmatic moves — rebuilds, mode toggles, the initial anchor — that this one does not).
        pass

    def on_merge_tree_cursor_moved(self, event: MergeTreeWidget.CursorMoved) -> None:
        # The cursor settled (any cause): preview where it is, and scroll the diagram so it stays visible.
        self._set_preview(event.node)
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
    # Preview box — a head/tail glimpse of the highlighted node's source text, recovering what the diagram
    # chip clipped. Tracks the cursor (``CursorMoved``); the full content is one quick-nav away.
    # ------------------------------------------------------------------

    def _set_preview(self, display_id) -> None:
        if self._preview is not None:
            self._preview.update(self._preview_text(display_id))

    def _preview_text(self, display_id) -> Text:
        source = self._vm.preview_for(display_id) if display_id is not None else ""
        return Text(self._compact(source) or "—", style="#b0b0b0")

    def _compact(self, text: str) -> str:
        """Head + tail of an over-long source with an ellipsis between; the whole text once it is short
        enough that the two would otherwise meet (see ``PREVIEW_CHARS`` / ``PREVIEW_BUFFER``)."""
        n = self.PREVIEW_CHARS
        if len(text) >= 2 * n + self.PREVIEW_BUFFER:
            return f"{text[:n]} … {text[-n:]}"
        return text

    # ------------------------------------------------------------------
    # Static content
    # ------------------------------------------------------------------

    def _header(self) -> Text:
        text = Text()
        text.append("Conversation Graph\n", style="bold")
        text.append(_DESC, style="#707070")
        return text

    _KEYHINT_PAIRS = (("↑↓←→", "navigate"), ("enter", "jump"), ("ctrl+e", "expand"))

    def _keyhint(self) -> Text:
        text = Text()
        for i, (key, action) in enumerate(self._KEYHINT_PAIRS):
            if i:
                text.append("   ")
            text.append(key, style="#a0a0a0")
            text.append(" ")
            text.append(action, style="#707070")
        return text
