"""``TopicTree`` — a multi-select topic tree over ``TopicTreeModel``.

The VM owns the whole eager-loaded forest plus the selection set (the resource filter); this view
owns the *visual* tree — which ``TreeNode``s exist, which are expanded, and where the cursor sits.
Checkboxes are painted off ``vm.selected(id)`` and ``space`` toggles ``vm.toggle_selected(id)`` (the
whole-subtree cascade lives in the VM).

``selectable`` (reactive, default True) turns the checkbox affordance on and off: when False the tree
drops the ``[ ]`` / ``[x]`` glyphs and the toggle action goes inert, so the same widget doubles as a
plain navigable topic tree — hence its home outside any one consumer.

VM → View is two channels: ``OnDataChanged`` → structural rebuild (preserving the cursor topic + the
expanded set across the reload), and ``OnSelectionChanged`` → a label-cache bust so the checkboxes
repaint. The tree structure is otherwise widget-owned and only changes through user expand/collapse.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from rhizome.app.resource_loader import TopicNode, TopicTreeModel
from rhizome.db import Topic
from rhizome.tui.keybindings import Keybind

_CHECKED_STYLE   = Style(color="rgb(100,200,100)")
_UNCHECKED_STYLE = Style(color="rgb(80,80,80)")
_CURSOR_FOCUSED  = Style(color="rgb(255,80,80)", bold=True)
_CURSOR_UNFOCUSED = Style(color="rgb(255,80,80)")
_ID_SUFFIX_STYLE = Style(color="rgb(120,120,120)")


class TopicTree(Tree[Topic]):
    """Multi-select (or plain) topic tree view. See module docstring."""

    selectable = reactive(True, init=False)
    """When False, render without checkboxes and make the toggle action inert."""

    BINDINGS = [
        # Cursor up/down are routed through the registry (the same keys ``Tree`` binds by default, now
        # tagged with their ``root.cursor_*`` ids so help / remapping can see them). left/right drive
        # our combined expand-and-move navigation; space toggles selection. shift+arrow sibling/parent
        # jumps are left inherited from ``Tree``.
        Keybind.CursorUp.as_binding("cursor_up", show=False),
        Keybind.CursorDown.as_binding("cursor_down", show=False),
        Keybind.CursorRight.as_binding("cursor_in", show=False),
        Keybind.CursorLeft.as_binding("cursor_out", show=False),
        Keybind.Toggle.as_binding("toggle_selection", show=False),
    ]

    DEFAULT_CSS = """
    TopicTree {
        background: transparent;
        padding: 0 0 0 1;
    }
    TopicTree:focus {
        background-tint: transparent;
    }
    /* Cursor reads as red label text, never a background block (matches the loader tree). */
    TopicTree > .tree--cursor,
    TopicTree:focus > .tree--cursor {
        background: transparent;
    }
    """

    def __init__(self, view_model: TopicTreeModel, *, selectable: bool = True, **kwargs: Any) -> None:
        super().__init__("Topics", **kwargs)
        self._vm = view_model
        self.show_root = False
        self.selectable = selectable

    # ------------------------------------------------------------------
    # Mount lifecycle + subscription wiring
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDataChanged, self._rebuild)
        self._vm.subscribe(self._vm.Callbacks.OnSelectionChanged, self._repaint)
        self._rebuild()  # paints whatever the VM already holds; a no-op until ``load`` lands.

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDataChanged, self._rebuild)
        self._vm.unsubscribe(self._vm.Callbacks.OnSelectionChanged, self._repaint)

    def watch_selectable(self) -> None:
        self._repaint()

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        # Structural reload: snapshot the cursor topic + expanded set, rebuild from ``vm.roots``, then
        # restore both so an out-of-band topic change doesn't yank the user's place in the tree.
        expanded = self._expanded_topic_ids()
        cursor = self.cursor_node
        cursor_id = cursor.data.id if cursor is not None and cursor.data is not None else None

        self.root.remove_children()
        self._add_nodes(self.root, self._vm.roots)

        self._restore_expansion(expanded)
        self._restore_cursor(cursor_id)
        self._repaint()

    def _repaint(self) -> None:
        # Bump ``_updates`` to bust Textual's render-line cache — a bare ``refresh()`` schedules a
        # repaint but doesn't invalidate, so the new checkbox/cursor styling wouldn't land.
        self._updates += 1
        self.refresh()

    def _add_nodes(self, parent: TreeNode[Topic], topic_nodes: list[TopicNode]) -> None:
        for tn in topic_nodes:
            if tn.children:
                child = parent.add(tn.name, data=tn.topic, allow_expand=True)
                self._add_nodes(child, tn.children)
            else:
                parent.add_leaf(tn.name, data=tn.topic)

    def _expanded_topic_ids(self) -> set[int]:
        ids: set[int] = set()

        def walk(node: TreeNode[Topic]) -> None:
            for child in node.children:
                if child.data is not None and child.is_expanded:
                    ids.add(child.data.id)
                walk(child)

        walk(self.root)
        return ids

    def _restore_expansion(self, ids: set[int]) -> None:
        if not ids:
            return

        def walk(node: TreeNode[Topic]) -> None:
            for child in node.children:
                if child.data is not None and child.data.id in ids:
                    child.expand()
                walk(child)

        walk(self.root)

    def _restore_cursor(self, topic_id: int | None) -> None:
        target = self._find_node(topic_id) if topic_id is not None else None
        if target is None and self.root.children:
            target = self.root.children[0]
        if target is not None:
            # ``move_cursor`` reads ``node._line``, only valid after the next build pass.
            self.call_after_refresh(self.move_cursor, target)

    def _find_node(self, topic_id: int) -> TreeNode[Topic] | None:
        def walk(node: TreeNode[Topic]) -> TreeNode[Topic] | None:
            for child in node.children:
                if child.data is not None and child.data.id == topic_id:
                    return child
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.root)

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def action_cursor_in(self) -> None:
        # right = expand a collapsed node, else step into its first child. Folding the move into one
        # key keeps space free for selection (``Tree`` binds expand/collapse to space by default).
        node = self.cursor_node
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0])

    def action_cursor_out(self) -> None:
        # left = collapse an expanded node, else step out to its parent.
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)

    def action_select_cursor(self) -> None:
        # Suppress the default enter behaviour: under ``auto_expand`` it would both toggle the node and
        # post ``NodeSelected`` (which DOM ancestors might act on). Selection here is via space.
        pass

    def action_toggle_selection(self) -> None:
        if not self.selectable:
            return
        node = self.cursor_node
        if node is None or node.data is None:
            return
        self._vm.toggle_selected(node.data.id)

    # ------------------------------------------------------------------
    # Label rendering — checkbox (when selectable) + cursor tint, off VM state
    # ------------------------------------------------------------------

    def render_label(self, node: TreeNode[Topic], base_style: Style, style: Style) -> Text:
        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon, icon_style = "", base_style

        parts: list[Any] = [(icon, icon_style)]

        if self.selectable and node.data is not None:
            checked = self._vm.selected(node.data.id)
            checkbox = "[x] " if checked else "[ ] "
            parts.append((checkbox, base_style + (_CHECKED_STYLE if checked else _UNCHECKED_STYLE)))

        # Focus-aware cursor tint so the user can tell whether keystrokes route here.
        if node is self.cursor_node:
            label_style = _CURSOR_FOCUSED if self.has_focus else _CURSOR_UNFOCUSED
        else:
            label_style = style
        label = node._label.copy()
        label.stylize(label_style)
        parts.append(label)

        if node.data is not None:
            parts.append((f" [{node.data.id}]", _ID_SUFFIX_STYLE))

        return Text.assemble(*parts)
