"""Inner tree widget and shared helpers for the ResourceLoader.

Separated from ``loader.py`` to keep the tree rendering logic distinct
from the outer container's state machine and manager communication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode, TOGGLE_STYLE

from rhizome.db import Resource
from rhizome.db.models import ResourceSection
from rhizome.resources import LoadMode, NodeKey

from rhizome.tui.types import Arrangement

if TYPE_CHECKING:
    from rhizome.tui.widgets.resource.loader import ResourceLoader


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# -- Colors ------------------------------------------------------------

_DIM = Style(color="rgb(100,100,100)")
_FOCUS_GREEN = Style(color="rgb(100,200,100)", bold=True)
_UNFOCUSED_BOLD = Style(bold=True)
_CHECKED_GREEN = Style(color="rgb(100,200,100)")
_CHECKED_AMBER = Style(color="rgb(220,170,50)")
_UNCHECKED = Style(color="rgb(80,80,80)")
_PENDING = Style(color="rgb(100,100,100)")
_PENDING_CURSOR = Style(color="rgb(140,140,140)")
_META = Style(color="rgb(80,80,80)")
_CTX_TAG = Style(color="rgb(220,170,50)")
_ID_STYLE = Style(color="rgb(80,80,80)")
_HINT_COLOR = "rgb(80,80,80)"

# Section depth colors: depth 1 is lighter, depth 2+ is dimmer.
_SECTION_DEPTH_1 = Style(color="rgb(140,140,140)")
_SECTION_DEPTH_2_PLUS = Style(color="rgb(100,100,100)")


# -- Shared helpers ----------------------------------------------------

NodeData = Resource | ResourceSection


def _fmt_tokens(n: int | None) -> str:
    """Format a token count as a short human-readable string."""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _state_key(data: NodeData) -> NodeKey:
    if isinstance(data, Resource):
        return ("resource", data.id)
    return ("section", data.id)


def _owning_resource(node: TreeNode[NodeData]) -> Resource:
    """Walk up to find the Resource that owns this node."""
    current = node
    while current is not None:
        if isinstance(current.data, Resource):
            return current.data
        current = current.parent
    raise RuntimeError("Section node has no Resource ancestor")


# -- Hint bar ----------------------------------------------------------

class LoaderHint(Static):
    """Self-rendering hint bar that reacts to load counts and arrangement."""

    loaded: reactive[int] = reactive(0)
    total: reactive[int] = reactive(0)
    sections: reactive[int] = reactive(0)
    vertical: reactive[bool] = reactive(False)

    _BINDINGS = [
        ("space", "toggle"),
        ("ctrl+enter", "context stuff"),
        ("\u2190/\u2192", "expand/collapse"),
    ]

    def render(self) -> str:
        summary = f"{self.loaded}/{self.total} loaded, {self.sections} sections"
        if self.vertical:
            key_width = max(len(k) for k, _ in self._BINDINGS)
            lines = [summary]
            for key, action in self._BINDINGS:
                lines.append(f"  {key:<{key_width}}  {action}")
            return "\n".join(lines)
        else:
            parts = [f"{k}: {a}" for k, a in self._BINDINGS]
            return f"{summary}  |  {'  '.join(parts)}"


# ======================================================================
# Inner tree widget
# ======================================================================

class LoaderTree(Tree[NodeData]):
    """The actual Tree — managed by the outer ResourceLoader container."""

    DEFAULT_CSS = """
    LoaderTree {
        height: auto;
        max-height: 20;
        margin: 1 1 1 1;
        background: transparent;
        overflow-y: auto;
    }
    LoaderTree:focus {
        background-tint: transparent;
    }
    LoaderTree > .tree--cursor {
        background: transparent;
    }
    LoaderTree:focus > .tree--cursor {
        background: transparent;
    }
    LoaderTree > .tree--highlight {
        background: transparent;
    }
    LoaderTree > .tree--highlight-line {
        background: transparent;
    }
    """

    def __init__(self, loader: ResourceLoader, **kwargs) -> None:
        super().__init__("Resources", **kwargs)
        self.show_root = False
        self._loader = loader

    def _refresh_height(self) -> None:
        line_count = len(self._tree_lines) + 2
        self.styles.height = max(line_count, 1)

    def _invalidate_label_cache(self) -> None:
        self._updates += 1
        self.refresh()

    # -- Expansion -----------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NodeData]) -> None:
        node = event.node
        data = node.data
        if data is None or node.children:
            self._refresh_height()
            return

        if isinstance(data, Resource):
            sections = getattr(data, "sections", None) or []
            root_sections = sorted(
                [s for s in sections if s.parent_id is None],
                key=lambda s: s.position,
            )
            for section in root_sections:
                child_sections = [s for s in sections if s.parent_id == section.id]
                if child_sections:
                    node.add(section.title, data=section, allow_expand=True)
                else:
                    node.add_leaf(section.title, data=section)

        elif isinstance(data, ResourceSection):
            resource = _owning_resource(node)
            all_sections = getattr(resource, "sections", None) or []
            children = sorted(
                [s for s in all_sections if s.parent_id == data.id],
                key=lambda s: s.position,
            )
            for child in children:
                grandchildren = [s for s in all_sections if s.parent_id == child.id]
                if grandchildren:
                    node.add(child.title, data=child, allow_expand=True)
                else:
                    node.add_leaf(child.title, data=child)

        self._refresh_height()

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed[NodeData]) -> None:
        self._refresh_height()

    # -- Key handling --------------------------------------------------

    def _on_key(self, event) -> None:
        if event.key == "right":
            node = self.cursor_node
            if node is not None and node.allow_expand:
                if not node.is_expanded:
                    node.expand()
                elif node.children:
                    self.move_cursor(node.children[0])
            event.stop()
            event.prevent_default()
        elif event.key == "left":
            node = self.cursor_node
            if node is not None:
                if node.is_expanded:
                    node.collapse()
                elif node.parent and node.parent is not self.root:
                    self.move_cursor(node.parent)
            event.stop()
            event.prevent_default()
        elif event.key in ("enter", "space"):
            event.stop()
            event.prevent_default()
            if event.key == "space":
                self._loader.action_toggle_default()
        else:
            super()._on_key(event)

    # -- Label rendering -----------------------------------------------

    def render_label(
        self, node: TreeNode[NodeData], base_style: Style, style: Style,
    ) -> Text:
        data = node.data
        if data is None:
            return Text(str(node._label))

        loader = self._loader
        key = _state_key(data)
        effective = loader._effective_mode(key)
        is_cursor = node is self.cursor_node

        # Determine whether this resource (or this section's owning resource)
        # is pending embedding.
        if isinstance(data, Resource):
            resource_pending = data.id in loader._pending_resources
            section_under_pending = False
        else:
            owning = _owning_resource(node)
            resource_pending = False
            section_under_pending = owning.id in loader._pending_resources

        # -- Pending state: spinner, no checkbox -----------------------
        if resource_pending:
            spinner = _SPINNER_FRAMES[loader._spinner_frame]
            return Text.assemble(
                (f"{spinner} ", _PENDING),
                (str(node._label), _PENDING),
                ("  computing embeddings...", _PENDING),
            )

        # -- Section under pending resource: greyed out, locked ---------
        if section_under_pending:
            label_style = _PENDING_CURSOR if is_cursor else _PENDING
            if node._allow_expand:
                icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
                icon_style = base_style + TOGGLE_STYLE
            else:
                icon = ""
                icon_style = base_style
            return Text.assemble(
                (icon, icon_style),
                ("[-] ", _PENDING),
                (str(node._label), label_style),
            )

        # -- Expand/collapse icon --------------------------------------
        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon = ""
            icon_style = base_style

        # -- Checkbox --------------------------------------------------
        partial = loader._is_partially_loaded(data)
        if effective is None and not partial:
            checkbox, cb_style = "[ ] ", _UNCHECKED
        elif effective == LoadMode.CONTEXT_STUFFED or (
            effective is None and loader._descendant_modes_only_cs(data)
        ):
            checkbox = "[/] " if partial else "[✓] "
            cb_style = _CHECKED_AMBER
        else:  # LOADED, or partial with at least one LOADED descendant
            checkbox = "[/] " if partial else "[✓] "
            cb_style = _CHECKED_GREEN

        # -- Name styling ----------------------------------------------
        if is_cursor and self.has_focus:
            name_style = _FOCUS_GREEN
        elif is_cursor:
            name_style = _UNFOCUSED_BOLD
        elif isinstance(data, ResourceSection):
            name_style = _SECTION_DEPTH_1 if data.depth <= 1 else _SECTION_DEPTH_2_PLUS
        else:
            name_style = style

        # -- Build suffix (metadata + ctx tag) so we know its width -----
        vertical = loader.dock_arrangement == Arrangement.VERTICAL
        suffix = ""
        if not vertical:
            if isinstance(data, Resource):
                meta_parts: list[str] = []
                if loader.show_ids:
                    meta_parts.append(f"[{data.id}]")
                meta_parts.append(f"~{_fmt_tokens(data.estimated_tokens)} tok")
                try:
                    chunk_count = len(data.chunks) if data.chunks is not None else 0
                except Exception:
                    chunk_count = 0
                meta_parts.append(f"{chunk_count} chunks")
                pref = data.loading_preference.value if data.loading_preference else "—"
                meta_parts.append(pref)
                suffix = "  " + " │ ".join(meta_parts)
            elif isinstance(data, ResourceSection):
                meta_parts: list[str] = []
                if loader.show_ids:
                    meta_parts.append(f"[{data.id}]")
                try:
                    chunk_count = len(data.chunks) if data.chunks is not None else 0
                except Exception:
                    chunk_count = 0
                if chunk_count:
                    meta_parts.append(f"{chunk_count} chunks")
                if meta_parts:
                    suffix = "  " + " │ ".join(meta_parts)

        if effective == LoadMode.CONTEXT_STUFFED:
            suffix += "  ctx"

        # -- Truncate name to fit within available width ---------------
        name = str(node._label)
        if not vertical:
            tree_depth = 0
            p = node.parent
            while p is not None:
                tree_depth += 1
                p = p.parent
            guide_width = self.guide_depth * tree_depth
            prefix_width = len(icon) + len(checkbox)
            available = self.size.width - guide_width - prefix_width - len(suffix) - 1
            available = max(available, 10)
            if len(name) > available:
                name = name[: available - 1] + "…"

        label = Text(name)
        label.stylize(name_style)

        text = Text.assemble(
            (icon, icon_style),
            (checkbox, base_style + cb_style),
            label,
        )

        # -- Append suffix with styling --------------------------------
        if not vertical:
            if isinstance(data, Resource):
                meta_end = suffix
                if effective == LoadMode.CONTEXT_STUFFED:
                    meta_end = suffix[: -len("  ctx")]
                    text.append(meta_end, style=_META)
                    text.append("  ctx", style=_CTX_TAG)
                else:
                    text.append(suffix, style=_META)
            else:
                if suffix:
                    meta_end = suffix
                    if effective == LoadMode.CONTEXT_STUFFED:
                        meta_end = suffix[: -len("  ctx")]
                        text.append(meta_end, style=_META)
                        text.append("  ctx", style=_CTX_TAG)
                    else:
                        text.append(suffix, style=_META)
                elif effective == LoadMode.CONTEXT_STUFFED:
                    text.append("  ctx", style=_CTX_TAG)
        elif suffix:
            # Vertical: only the ctx tag if present
            text.append(suffix, style=_CTX_TAG)

        return text
