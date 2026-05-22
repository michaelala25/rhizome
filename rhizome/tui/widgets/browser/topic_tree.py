"""BrowserTopicTreeViewModel — VM for the multi-select topic tree in the browser.

Responsibility split between View and VM in this module:

  * **VM owns persistent state**: the selection set, the cursor topic id
    (as an authoritative external reference other code can read without
    poking the widget), and the DB-facing operations (``fetch_children``,
    ``expanded_filter_ids``). The orchestrator subscribes to
    ``SELECTION_CHANGED`` to re-fan the filter.
  * **View owns the tree structure**: which TreeNodes exist, which are
    expanded, and the cursor *position* within the widget. This is purely
    visual state — Textual's ``Tree`` is excellent at it and there's no
    reason to mirror it onto the VM.

This split was deliberate: the original cut had the VM mirroring the tree
shape (a ``_roots`` list and a ``_children`` dict) in parallel with the
``Tree``'s own ``TreeNode`` structure, with constant sync work between
them. That duplication was a recurring source of bugs; collapsing it to
"VM is a DB facade + selection store, View owns the visual tree" removed
roughly a quarter of the file with no behavior change.

One consequence: the VM doesn't cache loaded children, so re-fetching the
same level after a collapse/re-expand cycle costs another query. For
SQLite + an indexed ``parent_id`` this is sub-millisecond; we'll add a
cache if it ever shows up in a profile.

Selection semantics: multi-select with **cascade-on-toggle**. Toggling a
topic expands its subtree via the recursive CTE and either adds the whole
subtree to the selection (if any descendant was missing — tri-state
"partial" counts as not-selected) or removes the whole subtree (if it was
already fully covered). The consequence is that ``_selected_ids`` *is*
the expanded filter set: there's no second-stage expansion at
filter-propagation time, every visible checkbox corresponds to an entry
that actually passes the filter, and the orchestrator's
selection-handler becomes a synchronous read.

Edge case: if you cascade-select A then explicitly uncheck a descendant
Y, A stays in ``_selected_ids`` but its subtree is no longer fully
covered. A subsequent toggle of A is treated as "not fully selected →
re-add the whole subtree" (standard tri-state file-picker behaviour),
which restores Y too.
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

# Style tokens for the view; centralized here so tweaks land in one place.
_CHECKED_STYLE = Style(color="rgb(100,200,100)")
_UNCHECKED_STYLE = Style(color="rgb(80,80,80)")
_CURSOR_FOCUSED = Style(color="rgb(255,80,80)", bold=True)
_CURSOR_UNFOCUSED = Style(color="rgb(255,80,80)")
# Trailing " [{id}]" hint after each topic name. Constant dim grey
# regardless of cursor / focus / selection state — it's metadata, not
# part of the topic label.
_ID_SUFFIX_STYLE = Style(color="rgb(120,120,120)")


@dataclass(frozen=True)
class LoadedTopic:
    """A topic plus a precomputed "has children?" hint, returned by
    ``BrowserTopicTreeViewModel.fetch_children``.

    The hint is the result of a single batched query against the just-loaded
    peer cohort (see ``find_parent_topic_ids``); the view uses it to decide
    whether each ``TreeNode`` should be added with ``allow_expand=True`` or
    as a leaf. Coupling the hint to the topic in the return value spares
    the view from making its own follow-up query.
    """
    topic: Topic
    has_children: bool


class BrowserTopicTreeViewModel(ViewModelBase):
    """View-model for the browser's multi-select topic tree.

    State here is intentionally minimal — see the module docstring for the
    rationale. The VM exposes selection / cursor state and two async DB
    methods (``fetch_children``, ``expanded_filter_ids``); everything else
    that *looks* like tree state (which nodes exist, which are expanded)
    lives on the view's ``TreeNode`` structure.
    """

    class Callbacks(Enum):
        # Standard dirty + focus are inherited; this one is browser-specific.
        # No payload — listeners re-query state via the public accessors.
        SELECTION_CHANGED = "selection_changed"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._selection_changed = self._make_group(
            BrowserTopicTreeViewModel.Callbacks.SELECTION_CHANGED
        )
        self._selected_ids: set[int] = set()
        # Cursor topic id is the *authoritative external reference* — kept
        # here so other code can ask "what topic is the user looking at?"
        # without poking the widget. The widget's own cursor position is
        # the source of truth for rendering; this mirrors it whenever the
        # view pushes a ``set_cursor`` update.
        self._cursor_topic_id: int | None = None

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def selection_changed(self):
        return self._selection_changed

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

    async def fetch_children(
        self,
        parent_id: int | None,
    ) -> list[LoadedTopic]:
        """Fetch the direct children of ``parent_id`` (or the roots when
        ``parent_id is None``), each paired with a ``has_children`` hint.

        Stateless from the VM's perspective: callers get the data back as
        the return value and are responsible for holding onto it as they
        see fit (the view stashes them inside ``TreeNode``s). Each call
        runs two queries: the list itself + a batched
        ``find_parent_topic_ids`` to populate the hints in one shot.
        """
        async with self._session_factory() as session:
            if parent_id is None:
                topics = await list_root_topics(session)
            else:
                topics = await list_children(session, parent_id)
            parent_set = await find_parent_topic_ids(
                session, [t.id for t in topics]
            )
        return [
            LoadedTopic(topic=t, has_children=t.id in parent_set)
            for t in topics
        ]

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    async def toggle_selection(self, topic_id: int) -> None:
        """Toggle ``topic_id`` with subtree cascade.

        Awaits a single recursive-CTE expansion to grab the full subtree
        rooted at ``topic_id``, then:

          * if every member of that subtree is already selected → remove
            them all (cascade-deselect);
          * otherwise → add them all (cascade-select; tri-state "partial"
            counts as not-fully-selected and re-adds everything,
            including any descendant the user previously unchecked).

        Emits ``dirty`` (the tree repaints every affected checkbox) and
        ``SELECTION_CHANGED`` (the orchestrator hands the new filter to
        the active pane). Both fire exactly once per toggle even though
        the cascade may move many ids.
        """
        async with self._session_factory() as session:
            subtree = await expand_subtrees(session, [topic_id])
        if subtree.issubset(self._selected_ids):
            self._selected_ids.difference_update(subtree)
        else:
            self._selected_ids.update(subtree)
        self.emit(self.dirty)
        self.emit(self._selection_changed)

    def clear_selection(self) -> None:
        """Drop all selections. No-op (and no emit) if already empty."""
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self.emit(self.dirty)
        self.emit(self._selection_changed)

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_cursor(self, topic_id: int | None) -> None:
        """Move the cursor to ``topic_id`` (or clear it). Emits ``dirty`` on
        actual change. Does not fire ``SELECTION_CHANGED`` — cursor and
        selection are independent."""
        if self._cursor_topic_id == topic_id:
            return
        self._cursor_topic_id = topic_id
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Filter projection
    # ------------------------------------------------------------------

    def expanded_filter_ids(self) -> frozenset[int] | None:
        """Return the topic-id filter the panes should apply.

        ``None`` means "no filter — show everything" (the empty-selection
        state). Otherwise returns ``_selected_ids`` as a frozenset.

        With cascade-on-toggle, the selection set is *already* the full
        subtree expansion, so this is a synchronous read — no CTE roundtrip
        at filter-propagation time.
        """
        if not self._selected_ids:
            return None
        return frozenset(self._selected_ids)


class BrowserTopicTreeView(Tree[Topic]):
    """View for ``BrowserTopicTreeViewModel``.

    Subclasses Textual's ``Tree`` to inherit node rendering, keyboard
    navigation, scrolling, expand/collapse, and cursor handling. The view
    owns the visual tree structure (``TreeNode``s, which are expanded,
    cursor position); the VM owns selection + the authoritative cursor id
    + DB operations. There is no duplicate tree state to keep in sync.

    Hooks added on top of ``Tree``:

      * **Multi-select checkboxes** drawn in ``render_label`` against the
        VM's ``is_selected``. ``Space`` toggles the VM's selection set.
      * **Lazy children load via the VM**: ``NodeExpanded`` fires the
        widget's natural event; our handler awaits
        ``vm.fetch_children(node.data.id)`` and stuffs the result into
        ``TreeNode``s. Re-expansion of an already-populated node is a
        no-op (we check ``node.children``).
      * **VM cursor sync**: ``NodeHighlighted`` forwards the cursor id back
        to the VM so other code can read it without poking the widget.
      * **Enter is suppressed**: selection happens via ``Space``;
        ``Tree.NodeSelected`` (Enter's default) isn't useful for this widget
        and would otherwise post a misleading message up the DOM.

    VM → View is only via ``dirty`` triggering a label-cache invalidation
    (selection / cursor styling repaints). Structural changes never come
    from the VM — they come from the user, via event handlers.
    """

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
        # Subscribe AFTER mount so ``_refresh`` can safely touch widget
        # internals (the Textual tree's line cache) without checking
        # ``is_mounted`` everywhere.
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # Load the roots ourselves — VM no longer caches tree shape.
        await self._populate_roots()

    def on_unmount(self) -> None:
        # Without this, a long-lived VM would keep firing into a dead widget.
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Repaint labels when the VM's selection or cursor id changes.

        No structural sync — the tree shape lives entirely on ``TreeNode``s
        and only mutates through user actions handled in our own event
        handlers.
        """
        self._invalidate_label_cache()

    async def _populate_roots(self) -> None:
        for lt in await self._vm.fetch_children(None):
            if lt.has_children:
                self.root.add(lt.topic.name, data=lt.topic, allow_expand=True)
            else:
                self.root.add_leaf(lt.topic.name, data=lt.topic)
        # Park the cursor on whichever root matches the VM's last-known
        # cursor; fall back to the first root. ``move_cursor`` will fire
        # ``NodeHighlighted`` which feeds back to the VM, but
        # ``vm.set_cursor`` is a no-op when the id is unchanged.
        target_id = self._vm.cursor_topic_id
        if target_id is not None:
            for node in self.root.children:
                if node.data is not None and node.data.id == target_id:
                    self.move_cursor(node)
                    return
        if self.root.children:
            self.move_cursor(self.root.children[0])

    def _invalidate_label_cache(self) -> None:
        """Bust Textual's tree-line render cache so ``render_label`` re-runs.

        Textual caches rendered lines keyed by ``_updates``; a bare
        ``refresh()`` schedules a repaint but doesn't invalidate the cache.
        Bumping ``_updates`` ensures the new checkbox / cursor style lands.
        Mirrors the trick from the legacy ``TopicTree``.
        """
        self._updates += 1
        self.refresh()

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    async def on_tree_node_expanded(self, event: Tree.NodeExpanded[Topic]) -> None:
        node = event.node
        if node.data is None:
            return
        # Already populated from a prior expand → no work to do. Textual
        # toggles the expand state on its own; we only fetch on first
        # expansion per node lifetime.
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

    async def action_toggle_selection(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        await self._vm.toggle_selection(node.data.id)

    def _on_key(self, event) -> None:
        # Custom right/left handling. Textual's default ``cursor_right`` /
        # ``cursor_left`` bindings work in some terminals but proved
        # unreliable under ``run_test`` (and arguably the legacy ``TopicTree``
        # pattern is what users have learned to expect). Mirror that:
        #   right: collapsed → expand; expanded → step into first child.
        #   left:  expanded → collapse; collapsed → step to parent.
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
        # Suppress Enter — selection is via Space, and the default
        # ``NodeSelected`` post would be misleading to any DOM ancestor.
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

        # Checkbox — drawn off VM state, not widget-local state.
        if node.data is not None:
            checked = self._vm.is_selected(node.data.id)
            checkbox = "[x] " if checked else "[ ] "
            checkbox_style = _CHECKED_STYLE if checked else _UNCHECKED_STYLE
        else:
            checkbox = ""
            checkbox_style = base_style

        # Cursor label tinting — focused vs. unfocused so the user can tell
        # whether keystrokes route here.
        is_cursor = node is self.cursor_node
        if is_cursor:
            label_style = _CURSOR_FOCUSED if self.has_focus else _CURSOR_UNFOCUSED
        else:
            label_style = style

        node_label = node._label.copy()
        node_label.stylize(label_style)

        # Trailing " [{id}]" — kept in a fixed dim grey so it reads as
        # metadata rather than part of the topic name. Skipped when
        # ``node.data`` is None (the synthetic root we hide via
        # ``show_root = False``, plus any defensive fallback nodes).
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
