"""Multi-select topic tree view. VM (``rhizome.app.browser.topics.tree.TopicTreeVM``) owns the
selection set + cursor topic id + DB reads; this view owns the visual tree (which ``TreeNode``s
exist, which are expanded, cursor position) by leaning on Textual's ``Tree``.

A synthetic ``(root)`` row sits at the top as a navigable placeholder for "no topic": highlighting
it pushes ``None`` into ``vm.set_cursor``, which downstream consumers read as "at the tree root".
It's a leaf with ``data=None``, so the same ``node.data is None`` guard the code already uses for
Textual's hidden root also identifies it everywhere we branch on data.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text

from textual import on
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from rhizome.app.browser.topics.tree import TopicTreeVM
from rhizome.tui.keybindings import Keybind
from rhizome.db import Topic

_CHECKED_STYLE = Style(color="rgb(100,200,100)")
_UNCHECKED_STYLE = Style(color="rgb(80,80,80)")
_CURSOR_FOCUSED = Style(color="rgb(255,80,80)", bold=True)
_CURSOR_UNFOCUSED = Style(color="rgb(255,80,80)")
_ID_SUFFIX_STYLE = Style(color="rgb(120,120,120)")
# Style for the synthetic ``(root)`` row at the top of the tree — a dimmer grey than regular
# topic names so it reads as a placeholder; cursor highlight uses the normal red tint.
_VIRTUAL_ROOT_STYLE = Style(color="rgb(100,100,100)")
_VIRTUAL_ROOT_LABEL = "(root)"


class TopicTree(Tree[Topic]):
    """Multi-select tree view. Adds: checkbox rendering off ``vm.is_selected``, ``Space`` toggle,
    lazy ``NodeExpanded`` → ``vm.fetch_children`` population, ``NodeHighlighted`` → ``vm.set_cursor``,
    and Enter suppression (selection is via Space; ``NodeSelected`` would mislead DOM ancestors).

    VM → View is only ``dirty`` → label-cache invalidation. Tree structure is widget-owned and only
    changes through user-driven event handlers."""

    BINDINGS = [
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
    TopicTree > .tree--cursor,
    TopicTree:focus > .tree--cursor {
        background: transparent;
    }
    """

    def __init__(self, view_model: TopicTreeVM, **kwargs: Any) -> None:
        super().__init__("Topics", **kwargs)
        self._vm = view_model
        self.show_root = False

    # ------------------------------------------------------------------
    # Mount lifecycle + subscription wiring
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        # Subscribe AFTER mount — ``_refresh`` touches widget internals (the tree's line cache).
        self._vm.subscribe(self._vm.dirty, self._refresh)
        await self._populate_roots()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._invalidate_label_cache()

    async def _populate_roots(self) -> None:
        # Synthetic ``(root)`` row at the top — selecting it parks the VM cursor on ``None``, which
        # downstream code (TopicDetailsVM, create-action dispatch) reads as "no parent / at
        # the tree root". Data is ``None`` to flag it as virtual everywhere we branch on data.
        self.root.add_leaf(_VIRTUAL_ROOT_LABEL, data=None)
        for lt in await self._vm.fetch_children(None):
            if lt.has_children:
                self.root.add(lt.topic.name, data=lt.topic, allow_expand=True)
            else:
                self.root.add_leaf(lt.topic.name, data=lt.topic)
        # Restore cursor: a remembered topic id wins; ``None`` (or no match) parks on ``(root)``.
        target_id = self._vm.cursor_topic_id
        if target_id is not None:
            for node in self.root.children:
                if node.data is not None and node.data.id == target_id:
                    self.move_cursor(node)
                    return
        if self.root.children:
            self.move_cursor(self.root.children[0])

    def _invalidate_label_cache(self) -> None:
        # Bump ``_updates`` to bust Textual's render-line cache — a bare ``refresh()`` schedules a
        # repaint but doesn't invalidate, so the new checkbox/cursor style wouldn't land.
        self._updates += 1
        self.refresh()

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(Tree.NodeExpanded)
    async def _on_node_expanded(self, event: Tree.NodeExpanded[Topic]) -> None:
        node = event.node
        if node.data is None:
            return
        # Skip if already populated — we only fetch on first expand per node.
        if node.children:
            return
        for lt in await self._vm.fetch_children(node.data.id):
            if lt.has_children:
                node.add(lt.topic.name, data=lt.topic, allow_expand=True)
            else:
                node.add_leaf(lt.topic.name, data=lt.topic)

    @on(Tree.NodeHighlighted)
    def _on_node_highlighted(self, event: Tree.NodeHighlighted[Topic]) -> None:
        # Synthetic ``(root)`` row → ``None`` cursor; real topic rows → their id.
        if event.node.data is None:
            self._vm.set_cursor(None)
            return
        self._vm.set_cursor(event.node.data.id)

    async def add_created_topic(self, topic: Topic) -> None:
        """Insert a freshly-created topic into the tree and park the cursor on it.

        Handles three parent shapes: ``None`` (append as a new root after any existing roots);
        an already-loaded parent (just append a leaf alongside the existing siblings); and a
        parent that was previously a childless leaf (flip ``allow_expand`` so the expand arrow
        renders, then append). For an expandable-but-unloaded parent we eagerly fetch its
        siblings first so the new node lands next to them rather than alone.
        """
        parent_id = topic.parent_id
        if parent_id is None:
            new_node = self.root.add_leaf(topic.name, data=topic)
            self.call_after_refresh(self.move_cursor, new_node)
            return

        parent_node = self._find_node(self.root, parent_id)
        if parent_node is None:
            # Parent isn't materialised in the tree (its ancestor subtree never expanded).
            # Nothing we can do without a deeper walk; the user can navigate to find it.
            return

        if not parent_node.allow_expand:
            # Was rendered as a leaf because it had no children at populate time. Promote it now.
            parent_node.allow_expand = True

        if not parent_node.children:
            # Either freshly-promoted or expandable-but-unloaded — fetch the full child set so
            # the new topic sits among its real siblings. The fetch reflects the just-committed
            # write so our row appears in the result.
            for lt in await self._vm.fetch_children(parent_id):
                if lt.has_children:
                    parent_node.add(lt.topic.name, data=lt.topic, allow_expand=True)
                else:
                    parent_node.add_leaf(lt.topic.name, data=lt.topic)
            new_node = next(
                (c for c in parent_node.children if c.data is not None and c.data.id == topic.id),
                None,
            )
        else:
            new_node = parent_node.add_leaf(topic.name, data=topic)

        if not parent_node.is_expanded:
            parent_node.expand()
        if new_node is not None:
            # ``move_cursor`` reads ``node._line`` which is only computed on the next render pass
            # — calling it synchronously after ``add_leaf`` / ``expand`` snaps the cursor to line
            # 0 (the synthetic ``(root)`` row) because ``_line`` is still its uninitialised
            # default. Defer to after the next refresh so the line cache is current.
            self.call_after_refresh(self.move_cursor, new_node)

    def remove_node(self, topic_id: int) -> bool:
        """Detach the node for ``topic_id`` (and its whole subtree, which Textual prunes for free)
        and move the cursor to its parent — or the synthetic ``(root)`` row when the deleted node
        was a top-level topic. Returns ``True`` when the node was found and removed."""
        node = self._find_node(self.root, topic_id)
        if node is None:
            return False
        parent = node.parent
        node.remove()
        if parent is None or parent is self.root:
            virtual = self.root.children[0] if self.root.children else None
            if virtual is not None:
                self.call_after_refresh(self.move_cursor, virtual)
        else:
            # Parent stops being expandable if it's now empty — flip the arrow off so it doesn't
            # look like it has hidden children.
            if not parent.children:
                parent.allow_expand = False
            self.call_after_refresh(self.move_cursor, parent)
        return True

    def update_node_label(self, topic_id: int, new_name: str) -> bool:
        """Find the node for ``topic_id`` and rewrite its label + ``data.name`` in place. Returns
        ``True`` if the node was found and updated. Used after a rename so the on-screen label
        matches the persisted value without a full reload."""
        target = self._find_node(self.root, topic_id)
        if target is None:
            return False
        if target.data is not None:
            target.data.name = new_name
        target.label = new_name  # type: ignore[assignment]
        self._invalidate_label_cache()
        return True

    def _find_node(self, node: "TreeNode[Topic]", topic_id: int):
        for child in node.children:
            if child.data is not None and child.data.id == topic_id:
                return child
            found = self._find_node(child, topic_id)
            if found is not None:
                return found
        return None

    async def action_toggle_selection(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        await self._vm.toggle_selection(node.data.id)

    def _on_key(self, event) -> None:
        # Custom horizontal arrows: right = expand-or-step-into-first-child, left = collapse-or-
        # step-to-parent. Textual's defaults proved unreliable under ``run_test``, and the explicit
        # tree-navigator pattern is what users expect anyway.
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
        # Suppress Enter — selection is via Space; default ``NodeSelected`` would mislead ancestors.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            return
        super()._on_key(event)  # pyright: ignore[reportUnusedCoroutine]

    # ------------------------------------------------------------------
    # Label rendering
    # ------------------------------------------------------------------

    def render_label(
        self,
        node: TreeNode[Topic],
        base_style: Style,
        style: Style,
    ) -> Text:
        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon = ""
            icon_style = base_style

        # Checkbox is drawn off VM state, not widget-local state.
        if node.data is not None:
            checked = self._vm.is_selected(node.data.id)
            checkbox = "[x] " if checked else "[ ] "
            checkbox_style = _CHECKED_STYLE if checked else _UNCHECKED_STYLE
        else:
            checkbox = ""
            checkbox_style = base_style

        # Focus-aware cursor tint so the user can tell whether keystrokes route here. The synthetic
        # ``(root)`` row uses a dimmer grey when idle but takes the standard red cursor tint when
        # highlighted so it doesn't visually disappear behind the cursor.
        is_cursor = node is self.cursor_node
        is_virtual = node.data is None
        if is_cursor:
            label_style = _CURSOR_FOCUSED if self.has_focus else _CURSOR_UNFOCUSED
        elif is_virtual:
            label_style = _VIRTUAL_ROOT_STYLE
        else:
            label_style = style

        node_label = node._label.copy()
        node_label.stylize(label_style)

        if node.data is not None:
            id_suffix = Text(f" [{node.data.id}]", style=_ID_SUFFIX_STYLE)
        else:
            id_suffix = Text("")

        return Text.assemble(
            (icon, icon_style),
            (checkbox, base_style + checkbox_style),
            node_label,
            id_suffix,
        )
