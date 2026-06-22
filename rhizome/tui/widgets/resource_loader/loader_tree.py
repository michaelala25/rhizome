"""``ResourceLoaderTree`` — the resource tree, rendered with the ``[IGL]`` load-state glyphs.

A ``MultilineTree`` over ``ResourceLoaderModel.roots``: each resource is a two-row root (name + a dim
token/section summary), each section a single row. Every node carries a compact glyph painted off
``vm.node_state`` — the loaded axes only, each its own colour, joined by ``|``:

    [-]    nothing loaded            (grey dash)
    [I]    indexed                   (I green)
    [G]    global context            (G yellow)
    [L]    local context             (L orange)
    [I|G]  indexed + global context
    [I|L]  indexed + local context

An axis shows only when it is on *and* its store is wired — the loader is a view of load state whose
source of truth lives elsewhere, so a missing channel honestly shows nothing (a bare ``[-]``).

Keys drive the two axes against the cursor node (load state is the VM's; the cursor is ours):

    space / i  → toggle the index axis
    g / l      → toggle global / local context (mutually exclusive)
    ctrl+enter → cycle context  NONE → LOCAL → GLOBAL → NONE  (skipping unwired channels)

VM → View: ``OnDataChanged`` rebuilds the tree (cursor + expansion preserved); ``OnLoadStateChanged``
repaints one resource's subtree glyphs (``None`` repaints every visible resource — e.g. after a
local-store swap). Navigation mirrors the topic tree (registry-routed cursor keys; left/right
expand-and-move; enter suppressed).
"""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from rhizome.app.resource_loader import ContextScope, ResourceDisplayNode, ResourceLoaderModel
from rhizome.resources_new import ResourceTreeNode
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.multiline_tree import MultilineTree

# Per-axis "on" colours; the off/unavailable slot and the brackets are dim grey.
_INDEX_ON   = Style(color="rgb(120,210,110)")   # green
_GLOBAL_ON  = Style(color="rgb(235,180,90)")    # yellow
_LOCAL_ON   = Style(color="rgb(235,140,60)")    # orange
_SLOT_OFF   = Style(color="rgb(90,90,90)")
_BRACKET    = Style(color="rgb(90,90,90)")
_INFO       = Style(color="rgb(110,110,110)")


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "? tokens"
    if n >= 1000:
        return f"{n / 1000:.1f}k tokens"
    return f"{n} tokens"


class ResourceLoaderTree(MultilineTree[ResourceDisplayNode]):
    """Load/unload resource tree over ``ResourceLoaderModel``. See module docstring."""

    BINDINGS = [
        Keybind.CursorUp.   as_binding("cursor_up",     show=False),
        Keybind.CursorDown. as_binding("cursor_down",   show=False),
        Keybind.CursorRight.as_binding("cursor_in",     show=False),
        Keybind.CursorLeft. as_binding("cursor_out",    show=False),
        Keybind.Toggle.     as_binding("toggle_index",  show=False),
        Keybind.ResourceToggleIndex. as_binding("toggle_index",  show=False),
        Keybind.ResourceToggleGlobal.as_binding("toggle_global", show=False),
        Keybind.ResourceToggleLocal. as_binding("toggle_local",  show=False),
        Keybind.ResourceToggleContext.as_binding("cycle_context", show=False),
    ]

    DEFAULT_CSS = """
    ResourceLoaderTree {
        background: transparent;
        padding: 0 1;
    }
    ResourceLoaderTree:focus {
        background-tint: transparent;
    }
    /* Cursor reads as bold-red text rather than a background block; the dim info row stays untinted. */
    ResourceLoaderTree > .tree--cursor,
    ResourceLoaderTree:focus > .tree--cursor {
        background: transparent;
        color: rgb(255, 80, 80);
        text-style: bold;
    }
    """

    def __init__(self, view_model: ResourceLoaderModel, **kwargs: Any) -> None:
        super().__init__("Resources", **kwargs)
        self._vm = view_model
        self.show_root = False
        # Cursor paints row 0 only (a resource's name row), leaving its dim info row untinted.
        self.highlight_full_node = False

    # ------------------------------------------------------------------
    # Mount lifecycle + subscription wiring
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDataChanged,      self._rebuild)
        self._vm.subscribe(self._vm.Callbacks.OnLoadStateChanged, self._on_load_changed)
        self._rebuild()  # paints whatever the VM already holds; a no-op until the first load lands.

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDataChanged,      self._rebuild)
        self._vm.unsubscribe(self._vm.Callbacks.OnLoadStateChanged, self._on_load_changed)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        # A fresh forest (load / search / topic filter / local-store swap): snapshot the cursor node
        # and expanded set, rebuild from ``roots``, then restore both so the user keeps their place.
        expanded = self._expanded_nodes()
        cursor = self.cursor_node
        cursor_key = cursor.data.node if cursor is not None and cursor.data is not None else None

        self.root.remove_children()
        for dnode in self._vm.roots:
            self._add_resource(dnode)

        self._restore_expansion(expanded)
        self._updates += 1
        self.refresh()
        self._restore_cursor(cursor_key)

    def _on_load_changed(self, resource_id: int | None) -> None:
        if resource_id is None:
            # Wholesale glyph change with no structural edit (e.g. local-store swap).
            self._updates += 1
            self.refresh()
            return
        tn = self._resource_node(resource_id)
        if tn is None:
            return
        # Bump the resource node's update counter (the line cache keys on every path node's counter,
        # so this busts the resource row and all its descendant rows) and repaint its visible lines.
        tn.refresh()
        self._refresh_node(tn)

    # ------------------------------------------------------------------
    # Tree construction
    # ------------------------------------------------------------------

    def _add_resource(self, dnode: ResourceDisplayNode) -> None:
        tn = self.root.add(dnode.label, data=dnode, allow_expand=bool(dnode.children))
        tn.height = 2  # name + info row
        for child in dnode.children:
            self._add_section(tn, child)

    def _add_section(self, parent: TreeNode[ResourceDisplayNode], dnode: ResourceDisplayNode) -> None:
        if dnode.children:
            ctn = parent.add(dnode.label, data=dnode, allow_expand=True)
            for child in dnode.children:
                self._add_section(ctn, child)
        else:
            parent.add_leaf(dnode.label, data=dnode)

    def _resource_node(self, resource_id: int) -> TreeNode[ResourceDisplayNode] | None:
        # Resources are always direct children of the (hidden) root.
        for child in self.root.children:
            if child.data is not None and child.data.is_resource and child.data.id == resource_id:
                return child
        return None

    def _expanded_nodes(self) -> set[ResourceTreeNode]:
        out: set[ResourceTreeNode] = set()

        def walk(node: TreeNode[ResourceDisplayNode]) -> None:
            for child in node.children:
                if child.data is not None and child.is_expanded:
                    out.add(child.data.node)
                walk(child)

        walk(self.root)
        return out

    def _restore_expansion(self, nodes: set[ResourceTreeNode]) -> None:
        if not nodes:
            return

        def walk(node: TreeNode[ResourceDisplayNode]) -> None:
            for child in node.children:
                if child.data is not None and child.data.node in nodes:
                    child.expand()
                walk(child)

        walk(self.root)

    def _restore_cursor(self, key: ResourceTreeNode | None) -> None:
        target = self._find_node(key) if key is not None else None
        if target is None and self.root.children:
            target = self.root.children[0]
        if target is not None:
            # ``move_cursor`` reads ``node._line``, only valid after the next build pass.
            self.call_after_refresh(self.move_cursor, target)

    def _find_node(self, key: ResourceTreeNode) -> TreeNode[ResourceDisplayNode] | None:
        def walk(node: TreeNode[ResourceDisplayNode]) -> TreeNode[ResourceDisplayNode] | None:
            for child in node.children:
                if child.data is not None and child.data.node == key:
                    return child
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.root)

    # ------------------------------------------------------------------
    # Label rendering — glyph + content per visible row, read from VM state
    # ------------------------------------------------------------------

    def render_label_lines(self, node: TreeNode[ResourceDisplayNode], base_style: Style, style: Style) -> list[Text]:
        dnode = node.data
        if dnode is None:  # hidden root — defer to the base
            return super().render_label_lines(node, base_style, style)

        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon, icon_style = "", base_style
        indent = " " * cell_len(icon)

        rendered: list[Text] = []
        for index, line in enumerate(self._content_lines(dnode, style)):
            prefix = (icon, icon_style) if index == 0 else (indent, base_style)
            rendered.append(Text.assemble(prefix, line))
        return rendered

    def _content_lines(self, dnode: ResourceDisplayNode, style: Style) -> list[Text]:
        glyph = self._glyph(dnode.node, style)
        if dnode.is_resource:
            name = glyph + Text(" ") + Text(dnode.label, style=style)
            info = Text("    " + self._info(dnode), style=style + _INFO)
            return [name, info]
        return [glyph + Text(" ") + Text(dnode.label, style=style)]

    def _glyph(self, node: ResourceTreeNode, style: Style) -> Text:
        # Compose each axis's colour on top of ``style`` (the cursor tint) so the glyph keeps its load
        # colour while sitting on the highlighted row. Loaded axes only, joined by ``|``; a node with
        # nothing loaded shows a single dash.
        state = self._vm.node_state(node)
        parts: list[tuple[str, Style]] = []
        if state.indexed:
            parts.append(("I", style + _INDEX_ON))
        if state.context is ContextScope.GLOBAL:
            parts.append(("G", style + _GLOBAL_ON))
        elif state.context is ContextScope.LOCAL:
            parts.append(("L", style + _LOCAL_ON))
        if not parts:
            parts.append(("-", style + _SLOT_OFF))

        body: list[tuple[str, Style]] = []
        for index, part in enumerate(parts):
            if index:
                body.append(("|", style + _BRACKET))
            body.append(part)
        return Text.assemble(("[", style + _BRACKET), *body, ("]", style + _BRACKET))

    def _info(self, dnode: ResourceDisplayNode) -> str:
        return f"{_fmt_tokens(dnode.estimated_tokens)} · {dnode.section_count} sections"

    # ------------------------------------------------------------------
    # View → VM — load toggles (the cursor node is passed in)
    # ------------------------------------------------------------------

    def action_toggle_index(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self._vm.toggle_index(node.data.node)

    def action_cycle_context(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self._vm.cycle_context(node.data.node)

    def action_toggle_global(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self._vm.toggle_context(node.data.node, ContextScope.GLOBAL)

    def action_toggle_local(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self._vm.toggle_context(node.data.node, ContextScope.LOCAL)

    # ------------------------------------------------------------------
    # Navigation (mirrors the topic tree)
    # ------------------------------------------------------------------

    def action_cursor_in(self) -> None:
        node = self.cursor_node
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0])

    def action_cursor_out(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)

    def action_select_cursor(self) -> None:
        # Suppress the default enter behaviour (expand + NodeSelected); load is via space / ctrl+enter.
        pass
