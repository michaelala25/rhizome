"""``ResourceLoaderTree`` — the loader VM's resources rendered as a load/unload tree.

A ``MultilineTree`` over ``ResourceLoaderModel``: each resource is a two-row root (name + a dim
token/section/chunk summary), each section a single row (title + a dim chunk count). Every node is
prefixed by a load-state glyph painted off ``vm.node_load_state``:

    [ ]  unloaded                       (dim grey)
    [x]  fully loaded — INDEX           (green)   / CONTEXT (amber)
    [/]  partially loaded               (green if every loaded descendant is indexed,
                                         amber if any descendant is context-loaded)

Two keys drive the load state; the full transition graph is a view concern, but it collapses to
"toggle the pressed type" against the node's current effective type:

    space      → toggle INDEX    (unloaded→index, index→unloaded, context→index)
    ctrl+enter → toggle CONTEXT  (unloaded→context, context→unloaded, index→context)

i.e. pressing a type that the node already *is* unloads it; pressing the other type switches to it.
``left`` / ``right`` collapse / expand (space is taken), mirroring the topic tree.

Rendering is dynamic: ``render_label_lines`` reads VM state per visible row, so a load toggle just
busts the line cache (``_refresh``) and the glyphs repaint. The tree's *structure* is rebuilt only
when the resource set changes (topic switch / link commit), detected by a cheap id signature.
"""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from textual import on
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from rhizome.app.resource_viewer.loader import NodeLoadState, ResourceLoaderModel, ResourceTreeNodeData
from rhizome.db import Resource
from rhizome.db.models import ResourceSection
from rhizome.resources import ResourceLoadType
from rhizome.utils.workers import WorkerSchedulerService
from rhizome.tui.widgets.shared.multiline_tree import MultilineTree
from rhizome.tui.keybindings import Keybind

# Load-state glyph colours. Green = indexed, amber = context-stuffed; dim grey for unloaded and for
# the secondary info text (token/section/chunk counts).
_INDEX_STYLE = Style(color="rgb(120,210,110)")
_CONTEXT_STYLE = Style(color="rgb(235,180,90)")
_UNLOADED_STYLE = Style(color="rgb(90,90,90)")
_INFO_STYLE = Style(color="rgb(110,110,110)")
_PENDING_STYLE = Style(color="rgb(150,150,150)")


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "? tokens"
    if n >= 1000:
        return f"{n / 1000:.1f}k tokens"
    return f"{n} tokens"


class ResourceLoaderTree(MultilineTree[ResourceTreeNodeData]):
    """Load/unload tree over ``ResourceLoaderModel``. See module docstring."""

    BINDINGS = [
        Keybind.Toggle.as_binding("toggle_index", show=False),
        # ctrl+enter toggles CONTEXT. ``ctrl+j`` is kept alongside as the legacy-terminal alias —
        # many terminals emit it for ctrl+enter (both are LF), so the binding still fires without the
        # enhanced keyboard protocol.
        Keybind.ResourceToggleContext.as_binding("toggle_context", show=False),
    ]

    DEFAULT_CSS = """
    ResourceLoaderTree {
        background: transparent;
        padding: 0 1;
    }
    ResourceLoaderTree:focus {
        background-tint: transparent;
    }
    /* Cursor reads as bold-red text rather than a background block — same red as the commit-proposal
       title. Both the blurred and the ``:focus`` variants are overridden (Textual styles each with a
       background by default), so the row keeps a transparent background in either state. */
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
        # Id signature of the tree as last built; a mismatch on ``dirty`` triggers a structural
        # rebuild rather than a glyph-only repaint.
        self._built_signature: tuple | None = None

    # ------------------------------------------------------------------
    # Mount lifecycle + subscription wiring
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # Bind embedding workers to this widget (the VM owns the coroutine; we supply the scheduler).
        self._vm.services.get(WorkerSchedulerService).bind(self.run_worker)
        self._vm.subscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        # Compare-and-clear so a remount that rebound first isn't clobbered by this late unmount.
        self._vm.services.get(WorkerSchedulerService).unbind(self.run_worker)
        self._vm.unsubscribe(self._vm.Callbacks.OnDirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        signature = self._structure_signature()
        if signature != self._built_signature:
            self._rebuild(signature)
        else:
            # Load state changed but the node set didn't — bust the render-line cache so the glyphs
            # repaint (a bare ``refresh()`` wouldn't invalidate the cached strips).
            self._updates += 1
            self.refresh()

    def _structure_signature(self) -> tuple:
        return tuple(
            (r.id, tuple(s.id for s in (r.sections or [])))
            for r in self._vm.resources
        )

    def _rebuild(self, signature: tuple) -> None:
        self.root.remove_children()
        for resource in self._vm.resources:
            rnode = self.root.add(resource.name, data=resource, allow_expand=bool(resource.sections))
            rnode.height = 2  # name + info row
            self._add_sections(rnode, resource)
        self._built_signature = signature
        self._updates += 1
        self.refresh()
        if self.root.children:
            # ``move_cursor`` reads ``node._line``, only valid after the next build pass.
            self.call_after_refresh(self.move_cursor, self.root.children[0])

    def _add_sections(self, resource_node: TreeNode, resource: Resource) -> None:
        # Build the section hierarchy from the flat ``resource.sections`` list via ``parent_id`` —
        # ``section.children`` is a lazy relationship we deliberately don't touch (the async session
        # is closed by now). Top-level sections have ``parent_id is None``.
        by_parent: dict[int | None, list[ResourceSection]] = {}
        for section in resource.sections or []:
            by_parent.setdefault(section.parent_id, []).append(section)

        def add_children(node: TreeNode, parent_section_id: int | None) -> None:
            for section in sorted(by_parent.get(parent_section_id, []), key=lambda s: s.position):
                if by_parent.get(section.id):
                    child = node.add(section.title, data=section, allow_expand=True)
                    add_children(child, section.id)
                else:
                    node.add_leaf(section.title, data=section)

        add_children(resource_node, None)

    # ------------------------------------------------------------------
    # Label rendering — glyph + content computed per visible row from VM state
    # ------------------------------------------------------------------

    def render_label_lines(self, node: TreeNode, base_style: Style, style: Style) -> list[Text]:
        data = node.data
        if data is None:  # hidden root — defer to the base
            return super().render_label_lines(node, base_style, style)

        # Expand/collapse icon on row 0; matching indent on continuation rows (mirrors the base).
        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon, icon_style = "", base_style
        indent = " " * cell_len(icon)

        rendered: list[Text] = []
        for index, line in enumerate(self._content_lines(data, style)):
            prefix = (icon, icon_style) if index == 0 else (indent, base_style)
            rendered.append(Text.assemble(prefix, line))
        return rendered

    def _content_lines(self, data: ResourceTreeNodeData, style: Style) -> list[Text]:
        # TODO(perf): ``node_load_state`` walks each node's subtree to resolve partial/colour, so a
        # full repaint is O(visible nodes · subtree). Only visible rows render, so it's fine for now;
        # if it bites, maintain a render-state map updated per load mutation instead of recomputing.
        glyph, glyph_color = self._glyph(self._vm.node_load_state(data))
        # ``style`` carries the cursor/hover tint; compose the glyph colour on top so the glyph keeps
        # its load colour while still sitting on the highlighted background.
        glyph_text = Text(glyph + " ", style=style + glyph_color)

        if isinstance(data, Resource):
            name = Text(data.name, style=style)
            info = Text("    " + self._resource_info(data), style=style + _INFO_STYLE)
            return [glyph_text + name, info]

        # ResourceSection — single row with a dim chunk count.
        title = Text(data.title, style=style)
        suffix = Text(f"  ({len(data.chunks or [])} chunks)", style=style + _INFO_STYLE)
        return [glyph_text + title + suffix]

    def _glyph(self, state: NodeLoadState) -> tuple[str, Style]:
        if state.pending:
            return "[~]", _PENDING_STYLE  # TODO: animated spinner while embeddings compute
        if state.load_type is ResourceLoadType.INDEX:
            return "[x]", _INDEX_STYLE
        if state.load_type is ResourceLoadType.CONTEXT:
            return "[x]", _CONTEXT_STYLE
        if state.partial:
            return "[/]", _CONTEXT_STYLE if state.partial_has_context else _INDEX_STYLE
        return "[ ]", _UNLOADED_STYLE

    def _resource_info(self, resource: Resource) -> str:
        n_sections = len(resource.sections or [])
        n_chunks = len(resource.chunks or [])
        return f"{_fmt_tokens(resource.estimated_tokens)} · {n_sections} sections · {n_chunks} chunks"

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(Tree.NodeHighlighted)
    def _on_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Mirror the highlighted node into the VM (feeds the preview). Identity-guarded VM-side.
        self._vm.set_cursor(event.node.data)

    # TODO: The spacebar action actually needs to consult the default loading preference of the current
    # resource, rather than just toggling ResourceLoadType.INDEX blindly - or is that going to be too
    # confusing a viewing experience for the end user?

    def action_toggle_index(self) -> None:
        self._toggle(ResourceLoadType.INDEX)

    def action_toggle_context(self) -> None:
        self._toggle(ResourceLoadType.CONTEXT)

    def _toggle(self, target_type: ResourceLoadType) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        data = node.data
        # Pressing the type the node already is → unload; otherwise load at the pressed type. This
        # one rule realises the full six-edge transition graph (see module docstring).
        if self._vm.effective_type(data) is target_type:
            self._vm.unload(data)
        else:
            self._vm.load(data, target_type)

    async def _on_key(self, event) -> None:
        # space/ctrl+j drive load state, so expand/collapse moves to left/right (matching the topic
        # tree). right = expand-or-step-into-first-child; left = collapse-or-step-to-parent. ``_on_key``
        # is a coroutine in this Textual version, so the fall-through must be awaited.
        if event.key == "right":
            node = self.cursor_node
            if node is not None and node.allow_expand:
                if not node.is_expanded:
                    node.expand()
                elif node.children:
                    self.move_cursor(node.children[0])
            event.stop()
            event.prevent_default()
            return
        if event.key == "left":
            node = self.cursor_node
            if node is not None:
                if node.is_expanded:
                    node.collapse()
                elif node.parent is not None and node.parent is not self.root:
                    self.move_cursor(node.parent)
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)
