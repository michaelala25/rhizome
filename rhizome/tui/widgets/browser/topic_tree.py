"""Multi-select topic tree for the browser's left rail. VM owns the selection set, the cursor
topic id (as an external reference), and the DB-facing reads; the view owns the visual tree
(which ``TreeNode``s exist, which are expanded, cursor position) by leaning on Textual's ``Tree``.

Selection is **cascade-on-toggle**: toggling a topic expands its subtree via the recursive CTE and
either adds or removes the whole subtree based on full-coverage. The consequence is that
``_selected_ids`` *is* the expanded filter set, so ``expanded_filter_ids()`` is a sync read with no
second-stage CTE at filter-propagation time. Partial coverage (cascade-add then explicitly uncheck
a descendant) counts as not-fully-selected, so a re-toggle re-adds the whole subtree — standard
tri-state file-picker behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.style import Style
from rich.text import Text

from textual.binding import Binding
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE, TreeNode

from rhizome.db import Topic
from rhizome.db.operations import (
    expand_subtrees,
    find_parent_topic_ids,
    list_children,
    list_root_topics,
)
from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase

_logger = get_logger("browser.topic_tree")

_CHECKED_STYLE = Style(color="rgb(100,200,100)")
_UNCHECKED_STYLE = Style(color="rgb(80,80,80)")
_CURSOR_FOCUSED = Style(color="rgb(255,80,80)", bold=True)
_CURSOR_UNFOCUSED = Style(color="rgb(255,80,80)")
_ID_SUFFIX_STYLE = Style(color="rgb(120,120,120)")


@dataclass(frozen=True)
class LoadedTopic:
    """A topic plus a precomputed ``has_children`` hint, returned by ``fetch_children``. The hint
    comes from a single batched ``find_parent_topic_ids`` against the peer cohort, sparing the view
    a follow-up query when it builds each ``TreeNode``."""
    topic: Topic
    has_children: bool


class BrowserTopicTreeViewModel(ViewModelBase):
    """Multi-select topic tree VM. Holds selection + cursor id + DB reads; the view holds the rest."""

    class Callbacks(Enum):
        # No payloads — listeners read public accessors. ``CURSOR_CHANGED`` is split from ``dirty``
        # so consumers like the topic-summary panel don't refetch on every selection-toggle repaint.
        SELECTION_CHANGED = "selection_changed"
        CURSOR_CHANGED = "cursor_changed"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._selection_changed = self._make_group(
            BrowserTopicTreeViewModel.Callbacks.SELECTION_CHANGED
        )
        self._cursor_changed = self._make_group(
            BrowserTopicTreeViewModel.Callbacks.CURSOR_CHANGED
        )
        self._selected_ids: set[int] = set()
        # Authoritative external reference; mirrors the widget's own cursor whenever the view pushes
        # a ``set_cursor``. Other code reads it without poking the widget.
        self._cursor_topic_id: int | None = None

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def selection_changed(self):
        return self._selection_changed

    @property
    def cursor_changed(self):
        return self._cursor_changed

    def is_selected(self, topic_id: int) -> bool:
        return topic_id in self._selected_ids

    @property
    def selected_ids(self) -> frozenset[int]:
        return frozenset(self._selected_ids)

    @property
    def cursor_topic_id(self) -> int | None:
        return self._cursor_topic_id

    # ------------------------------------------------------------------
    # DB-facing operations
    # ------------------------------------------------------------------

    async def fetch_children(self, parent_id: int | None) -> list[LoadedTopic]:
        """Direct children of ``parent_id`` (or the roots when ``None``), each with a ``has_children``
        hint from a batched ``find_parent_topic_ids``. Stateless — the view holds the result inside
        ``TreeNode``s rather than the VM caching it."""
        async with self._session_factory() as session:
            if parent_id is None:
                topics = await list_root_topics(session)
            else:
                topics = await list_children(session, parent_id)
            parent_set = await find_parent_topic_ids(session, [t.id for t in topics])
        return [LoadedTopic(topic=t, has_children=t.id in parent_set) for t in topics]

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    async def toggle_selection(self, topic_id: int) -> None:
        """Toggle ``topic_id`` with subtree cascade — expand once via the CTE, then add the whole
        subtree if any descendant was missing or remove the whole subtree if it was fully covered.
        Emits ``dirty`` + ``SELECTION_CHANGED`` exactly once even when the cascade moves many ids."""
        async with self._session_factory() as session:
            subtree = await expand_subtrees(session, [topic_id])
        if subtree.issubset(self._selected_ids):
            self._selected_ids.difference_update(subtree)
        else:
            self._selected_ids.update(subtree)
        self.emit(self.dirty)
        self.emit(self._selection_changed)

    def clear_selection(self) -> None:
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self.emit(self.dirty)
        self.emit(self._selection_changed)

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_cursor(self, topic_id: int | None) -> None:
        if self._cursor_topic_id == topic_id:
            return
        self._cursor_topic_id = topic_id
        self.emit(self.dirty)
        self.emit(self._cursor_changed)

    # ------------------------------------------------------------------
    # Filter projection
    # ------------------------------------------------------------------

    def expanded_filter_ids(self) -> frozenset[int] | None:
        """``None`` for empty selection (no filter); otherwise the selection set as a frozenset.
        Sync read — cascade-on-toggle has already done the subtree expansion."""
        if not self._selected_ids:
            return None
        return frozenset(self._selected_ids)


class BrowserTopicTreeView(Tree[Topic]):
    """Multi-select tree view. Adds: checkbox rendering off ``vm.is_selected``, ``Space`` toggle,
    lazy ``NodeExpanded`` → ``vm.fetch_children`` population, ``NodeHighlighted`` → ``vm.set_cursor``,
    and Enter suppression (selection is via Space; ``NodeSelected`` would mislead DOM ancestors).

    VM → View is only ``dirty`` → label-cache invalidation. Tree structure is widget-owned and only
    changes through user-driven event handlers."""

    BINDINGS = [
        Binding("space", "toggle_selection", show=False),
    ]

    DEFAULT_CSS = """
    BrowserTopicTreeView {
        background: transparent;
        padding: 0 0 0 1;
    }
    BrowserTopicTreeView:focus {
        background-tint: transparent;
    }
    BrowserTopicTreeView > .tree--cursor,
    BrowserTopicTreeView:focus > .tree--cursor {
        background: transparent;
    }
    """

    def __init__(self, view_model: BrowserTopicTreeViewModel, **kwargs: Any) -> None:
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
        for lt in await self._vm.fetch_children(None):
            if lt.has_children:
                self.root.add(lt.topic.name, data=lt.topic, allow_expand=True)
            else:
                self.root.add_leaf(lt.topic.name, data=lt.topic)
        # Restore cursor to the VM's last-known topic if present; otherwise park on the first root.
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

    async def on_tree_node_expanded(self, event: Tree.NodeExpanded[Topic]) -> None:
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

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[Topic]) -> None:
        if event.node.data is None:
            return
        self._vm.set_cursor(event.node.data.id)

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

        # Focus-aware cursor tint so the user can tell whether keystrokes route here.
        is_cursor = node is self.cursor_node
        if is_cursor:
            label_style = _CURSOR_FOCUSED if self.has_focus else _CURSOR_UNFOCUSED
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
