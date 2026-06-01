"""A `Tree` subclass whose nodes may span a fixed (per-node) number of rows.

Textual's `Tree` is a Line API widget: the compositor only ever asks it for "the strip at
render-row `y`", and the scrollable height is whatever `virtual_size.height` says. The base widget
relies on there being exactly one render-row per node, so a single integer doubles as both a
*node index* and a *render-row*. Multiline nodes break that coincidence.

The internal model stays entirely in NODE-SPACE (every "line" is still the Nth visible node, as in
the base widget); translation to RENDER-SPACE happens only at the rendering / scroll / refresh seam.
The bridge is rebuilt once per `_build()`:

    _row_offsets[i]   first render-row of node i      (prefix sum of node heights)
    _row_to_node[y]   node index that owns render-row y
    virtual_size.h    == sum of node heights

Because cursor/hover/navigation all stay in node-space, `cursor_line`, the `action_cursor_*`
handlers, mouse hit-testing (`meta["line"]`), and multi-row cursor highlighting need no special
handling — they fall out of comparing against the node index rather than the render-row.

A node's height defaults to the number of newline-separated lines in its label; set `node.height = n`
to pin it explicitly (text is padded with blanks or truncated to fit).

Example:
    ```python
    tree = MultilineTree("Rhizome\n[dim]knowledge base[/dim]")    # 2-row root
    tree.root.expand()

    topics = tree.root.add("Topics\n[dim]subjects[/dim]", expand=True)
    topics.add_leaf("Python\n• async / await\n• type hints")      # 3-row leaf
    note = topics.add_leaf("Pinned note")
    note.height = 4                                               # pad out to 4 rows

    tree.highlight_full_node = False    # cursor highlights row 0 only, not the whole body
    ```
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.style import NULL_STYLE, Style
from rich.text import Text, TextType

from textual._loop import loop_last
from textual._segment_tools import line_pad
from textual.geometry import Region, Size
from textual.reactive import reactive
from textual.strip import Strip
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE, TreeDataType, TreeNode, _TreeLine


# ======================================================================================================
# NODE
# ======================================================================================================

class MultilineTreeNode(TreeNode[TreeDataType]):
    """A tree node that remembers how many render-rows it should occupy.

    `height` defaults to the label's line count; assigning it pins an explicit height (and triggers a
    rebuild). The full multi-line label is preserved — `MultilineTree.process_label` keeps every line
    rather than truncating to the first.
    """

    def __init__(self, *args, height: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._explicit_height = height

    @property
    def height(self) -> int:
        if self._explicit_height is not None:
            return max(1, self._explicit_height)
        return max(1, len(self._label.split("\n")))

    @height.setter
    def height(self, value: int | None) -> None:
        self._explicit_height = None if value is None else max(1, value)
        self._tree._invalidate()


# ======================================================================================================
# TREE
# ======================================================================================================

class MultilineTree(Tree[TreeDataType]):
    """A `Tree` whose nodes may occupy more than one render-row each."""

    highlight_full_node = reactive(True, init=False)
    """If `True` the cursor/hover highlight covers every row of a node; if `False`, only row 0."""

    def __init__(self, *args, **kwargs) -> None:
        # The render-space bridge. Populated in `_build`; seeded here so methods that may run before
        # the first build (e.g. an early refresh) see something coherent.
        self._row_offsets: list[int] = [0]
        self._row_to_node: list[int] = []
        super().__init__(*args, **kwargs)

    def watch_highlight_full_node(self) -> None:
        # The highlight choice is baked into cached strips, so drop the cache. Heights are unchanged,
        # hence no rebuild/relayout — just a repaint.
        self._line_cache.clear()
        self.refresh()

    # ------------------------------------------------------------------
    # Labels / heights
    # ------------------------------------------------------------------

    def process_label(self, label: TextType) -> Text:
        """Keep the whole label (the base `Tree` truncates to the first line here)."""
        return Text.from_markup(label) if isinstance(label, str) else label

    def _node_height(self, node: TreeNode[TreeDataType]) -> int:
        return max(1, getattr(node, "height", 1))

    def _add_node(self, parent, label, data, expand: bool = False):
        node = MultilineTreeNode(self, parent, self._new_id(), label, data, expanded=expand)
        self._tree_nodes[node._id] = node
        self._updates += 1
        return node

    def render_label_lines(
        self, node: TreeNode[TreeDataType], base_style: Style, style: Style
    ) -> list[Text]:
        """Render a node's label as exactly `node.height` rows.

        Row 0 carries the expand/collapse icon; continuation rows are indented to match so the text
        columns line up. Rows beyond the label's content are blank.
        """
        label = node._label.copy()
        label.stylize(style)
        label_lines = list(label.split("\n"))

        height = self._node_height(node)
        if len(label_lines) < height:
            label_lines += [Text("", style=style)] * (height - len(label_lines))
        else:
            label_lines = label_lines[:height]

        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon, icon_style = "", base_style
        indent = " " * cell_len(icon)

        rendered: list[Text] = []
        for index, text_line in enumerate(label_lines):
            prefix = (icon, icon_style) if index == 0 else (indent, base_style)
            rendered.append(Text.assemble(prefix, text_line))
        return rendered

    def get_label_width(self, node: TreeNode[TreeDataType]) -> int:
        """Widest rendered row of the node (drives virtual width + label region)."""
        lines = self.render_label_lines(node, NULL_STYLE, NULL_STYLE)
        return max((line.cell_len for line in lines), default=0)

    # ------------------------------------------------------------------
    # Build / coordinate bridge
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # The render-space bridge is populated in the SAME pass as the node-space lines: the
        # `cursor_line` write below fires `watch_cursor_line` → `_refresh_node` synchronously, and
        # that handler reads both structures. Building them separately would expose a stale table.
        lines: list[_TreeLine[TreeDataType]] = []
        add_line = lines.append

        def add_node(path, node, last: bool) -> None:
            child_path = [*path, node]
            node._line = len(lines)
            add_line(_TreeLine(child_path, last))
            if node._expanded:
                for last_child, child in loop_last(node._children):
                    add_node(child_path, child, last_child)

        if self.show_root:
            add_node([], self.root, True)
        else:
            for node in self.root._children:
                add_node([], node, True)

        offsets: list[int] = []
        row_to_node: list[int] = []
        row = 0
        for index, line in enumerate(lines):
            offsets.append(row)
            row_to_node.extend([index] * self._node_height(line.node))
            row += self._node_height(line.node)
        offsets.append(row)  # sentinel: total render-row count

        self._tree_lines_cached = lines
        self._row_offsets = offsets
        self._row_to_node = row_to_node

        if lines:
            guide_depth, show_root = self.guide_depth, self.show_root
            width = max(
                self.get_label_width(line.node) + line._get_guide_width(guide_depth, show_root)
                for line in lines
            )
        else:
            width = self.size.width

        self.virtual_size = Size(width, row)

        if self.cursor_line != -1:
            if self.cursor_node is not None:
                self.cursor_line = self.cursor_node._line
            if self.cursor_line >= len(lines):
                self.cursor_line = -1

    # ------------------------------------------------------------------
    # Rendering · render-space → node-space
    # ------------------------------------------------------------------

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        tree_lines = self._tree_lines  # forces a build if needed (populates the bridge)
        width = self.size.width

        if y < 0 or y >= len(self._row_to_node):
            return Strip.blank(width, base_style)

        node_index = self._row_to_node[y]
        row = y - self._row_offsets[node_index]
        line = tree_lines[node_index]

        is_hover = self.hover_line >= 0 and any(node._hover for node in line.path)

        cache_key = (
            y,
            is_hover,
            width,
            self._updates,
            self._pseudo_class_state,
            tuple(node._updates for node in line.path),
        )
        if cache_key in self._line_cache:
            strip = self._line_cache[cache_key]
        else:
            base_hidden = self.get_component_styles("tree--guides").color.a == 0
            hover_hidden = self.get_component_styles("tree--guides-hover").color.a == 0
            selected_hidden = self.get_component_styles("tree--guides-selected").color.a == 0

            base_guide_style = self.get_component_rich_style("tree--guides", partial=True)
            guide_hover_style = base_guide_style + self.get_component_rich_style(
                "tree--guides-hover", partial=True
            )
            guide_selected_style = base_guide_style + self.get_component_rich_style(
                "tree--guides-selected", partial=True
            )

            hover = line.path[0]._hover
            selected = line.path[0]._selected and self.has_focus

            def get_guides(style: Style, hidden: bool) -> tuple[str, str, str, str]:
                if self.show_guides and not hidden:
                    lines = self.LINES["default"]
                    if style.bold:
                        lines = self.LINES["bold"]
                    elif style.underline2:
                        lines = self.LINES["double"]
                else:
                    lines = ("  ", "  ", "  ", "  ")
                guide_depth = max(0, self.guide_depth - 2)
                return tuple(f"{chars[0]}{chars[1] * guide_depth} " for chars in lines)

            line_style = (
                self.get_component_rich_style("tree--highlight-line") if is_hover else base_style
            )
            # Hit-testing meta is the NODE INDEX, not the render-row, so mouse handlers keep working.
            line_style += Style(meta={"line": node_index})

            guides = Text(style=line_style)
            guide_style = base_guide_style
            hidden = True
            for node in line.path[1:]:
                hidden = base_hidden
                if hover:
                    guide_style = guide_hover_style
                    hidden = hover_hidden
                if selected:
                    guide_style = guide_selected_style
                    hidden = selected_hidden
                space, vertical, _, _ = get_guides(guide_style, hidden)
                if node != line.path[-1]:
                    guides.append(space if node.is_last else vertical, style=guide_style)
                hover = hover or node._hover
                selected = (selected or node._selected) and self.has_focus

            # The node's own connector occupies one guide column. Row 0 draws the branch glyph;
            # continuation rows extend the vertical bar (unless the node is its parent's last child).
            if len(line.path) > 1:
                space, vertical, terminator, cross = get_guides(guide_style, hidden)
                if row == 0:
                    guides.append(terminator if line.last else cross, style=guide_style)
                else:
                    guides.append(space if line.last else vertical, style=guide_style)

            label_style = self.get_component_rich_style("tree--label", partial=True)
            highlight_row = self.highlight_full_node or row == 0
            if highlight_row and self.hover_line == node_index:
                label_style += self.get_component_rich_style("tree--highlight", partial=True)
            if highlight_row and self.cursor_line == node_index:
                label_style += self.get_component_rich_style("tree--cursor", partial=False)

            label = self.render_label_lines(line.path[-1], line_style, label_style)[row].copy()
            label.stylize(Style(meta={"node": line.node._id}))
            guides.append(label)

            segments = list(guides.render(self.app.console))
            pad_width = max(self.virtual_size.width, width)
            segments = line_pad(segments, 0, pad_width - guides.cell_len, line_style)
            strip = self._line_cache[cache_key] = Strip(segments)

        return strip.crop(x1, x2)

    # ------------------------------------------------------------------
    # Scroll / refresh · node-space → render-space
    # ------------------------------------------------------------------

    def _get_label_region(self, line: int) -> Region | None:
        try:
            tree_line = self._tree_lines[line]
        except IndexError:
            return None
        region_x = tree_line._get_guide_width(self.guide_depth, self.show_root)
        region_width = self.get_label_width(tree_line.node)
        return Region(region_x, self._row_offsets[line], region_width, self._node_height(tree_line.node))

    def _refresh_line(self, line: int) -> None:
        try:
            tree_line = self._tree_lines[line]
        except IndexError:
            return
        top = self._row_offsets[line] - self.scroll_offset.y
        self.refresh(Region(0, top, self.size.width, self._node_height(tree_line.node)))

    def _refresh_node(self, node: TreeNode[TreeDataType]) -> None:
        scroll_y = self.scroll_offset.y
        window_bottom = scroll_y + self.size.height
        for index, line in enumerate(self._tree_lines):
            top = self._row_offsets[index]
            bottom = top + self._node_height(line.node)
            if bottom <= scroll_y or top >= window_bottom:
                continue  # node not in the visible render window
            if node in line.path:
                self._refresh_line(index)

    # ------------------------------------------------------------------
    # Paging · step by nodes spanning a page of rows
    # ------------------------------------------------------------------

    def action_page_down(self) -> None:
        if not self._row_to_node:
            return
        if self.cursor_line == -1:
            self.cursor_line = 0
        else:
            page = max(1, self.scrollable_content_region.height - 1)
            target = min(self._row_offsets[self.cursor_line] + page, len(self._row_to_node) - 1)
            self.cursor_line = self._row_to_node[target]
        self.scroll_to_line(self.cursor_line)

    def action_page_up(self) -> None:
        if not self._row_to_node:
            return
        if self.cursor_line == -1:
            self.cursor_line = self.last_line
        else:
            page = max(1, self.scrollable_content_region.height - 1)
            target = max(self._row_offsets[self.cursor_line] - page, 0)
            self.cursor_line = self._row_to_node[target]
        self.scroll_to_line(self.cursor_line)
