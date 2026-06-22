"""Topic-tree VM for the resource viewer's filter rail.

Holds the entire topic tree (eager-loaded once via :meth:`load`) plus a multi-select **selection**
that expresses the resource filter. The tree is small, so we load it whole and build the
parent/child structure in memory rather than expanding lazily per node.

State / cursor split
--------------------
Per the project's MVVM split, this VM owns only *data + committed state*: the tree and the
selection set. The **cursor** (which node is highlighted) and **expand/collapse** are the view's
concern — there is deliberately no ``cursor`` field, no ``set_cursor``, and no ``OnCursorChanged``
here. A view action that mutates the selection passes the target ``topic_id`` in directly
(:meth:`toggle_selected`), the same way ``CommitProposalModel`` takes an explicit index.

Selection is the filter
-----------------------
Selection is **cascade-on-toggle**: selecting a topic pulls in its whole subtree, so
``_selected_ids`` *is* the filter and :meth:`selected_ids` needs no second-stage expansion. Because
the whole tree is in memory, the subtree walk is in-memory too (no recursive CTE). An empty
selection means "no filter" — cascade guarantees a non-empty selection always matches something, so
there's no empty-yet-filtering state to distinguish. The orchestrator subscribes to
``OnSelectionChanged`` to refetch the resource loader; the view subscribes to repaint checkboxes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rhizome.app.model import ViewModelBase
from rhizome.db import Topic
from rhizome.db.operations import list_all_topics


@dataclass(frozen=True)
class TopicNode:
    """One node in the in-memory topic tree: a ``Topic`` plus its already-resolved children.

    Built once by :meth:`TopicTreeModel._build_tree` so the view can walk the structure without
    touching lazy ORM relationships on detached instances (only column attributes are read)."""

    topic: Topic
    children: list["TopicNode"] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.topic.id

    @property
    def name(self) -> str:
        return self.topic.name


class TopicTreeModel(ViewModelBase):
    """Eager topic tree + multi-select filter. See module docstring."""

    class Callbacks(ViewModelBase.Callbacks):
        # Tree (re)loaded — the view rebuilds its tree. Selection filter changed — the orchestrator
        # refetches the loader and the view repaints checkboxes. Split so a selection toggle doesn't
        # read as a structural reload.
        OnDataChanged      = "OnDataChanged"
        OnSelectionChanged = "OnSelectionChanged"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self.make_callback_groups({
            self.Callbacks.OnDataChanged:      None,
            self.Callbacks.OnSelectionChanged: None,
        })

        # The tree as roots → children. Empty until ``load`` resolves.
        self._roots: list[TopicNode] = []
        # Flat id → child-ids adjacency, for the in-memory subtree walk (cascade selection). Every
        # topic id is a key, so its key set doubles as "all live topic ids".
        self._child_ids: dict[int, list[int]] = {}

        # The multi-select filter. Cascade keeps this equal to the union of selected subtrees, so
        # ``selected_ids`` needs no further expansion.
        self._selected_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def roots(self) -> list[TopicNode]:
        """The root topics (name-ordered). The view walks these to build its tree."""
        return self._roots

    def selected(self, topic_id: int) -> bool:
        """Whether ``topic_id`` itself is in the selection (the checkbox state the view paints)."""
        return topic_id in self._selected_ids

    @property
    def selected_ids(self) -> frozenset[int]:
        """The filter: the current selection (the toggle gesture pulls in whole subtrees). Empty ==
        no filter."""
        return frozenset(self._selected_ids)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Fetch the whole topic tree and (re)build the in-memory structure. Re-callable to refresh
        after out-of-band topic changes; the selection survives, pruned to surviving ids. Emits
        ``OnSelectionChanged`` as well when that prune actually dropped a filtered id."""
        async with self._session_factory() as session:
            topics = await list_all_topics(session)
        self._build_tree(topics)

        before = set(self._selected_ids)
        self._selected_ids &= set(self._child_ids)
        self.emit(self.Callbacks.OnDataChanged)
        if self._selected_ids != before:
            self.emit(self.Callbacks.OnSelectionChanged)

    # ------------------------------------------------------------------
    # Selection (the filter)
    # ------------------------------------------------------------------

    def set_selected(self, topic_ids: int | list[int], selected: bool, cascade: bool = True) -> None:
        """Set the selected-state of ``topic_ids`` (one id or a list) to ``selected`` — add them to,
        or remove them from, the selection (it does *not* replace the whole set). With ``cascade``
        (default) each id carries its whole subtree; otherwise only the listed ids move. Ids outside
        the tree are ignored. Emits ``OnSelectionChanged`` only when the set actually changes."""
        ids = [topic_ids] if isinstance(topic_ids, int) else list(topic_ids)
        if cascade:
            targets = set().union(*(self._subtree_ids(i) for i in ids)) if ids else set()
        else:
            targets = {i for i in ids if i in self._child_ids}
        new = self._selected_ids | targets if selected else self._selected_ids - targets
        if new == self._selected_ids:
            return
        self._selected_ids = new
        self.emit(self.Callbacks.OnSelectionChanged)

    def toggle_selected(self, topic_id: int, cascade: bool = True) -> None:
        """Convenience over :meth:`set_selected` + :meth:`selected`: flip ``topic_id``'s subtree —
        deselect it if the node is currently selected, else select it. The view passes the cursor's
        topic id in directly."""
        self.set_selected(topic_id, not self.selected(topic_id), cascade=cascade)

    def clear_selection(self) -> None:
        """Drop the whole selection (clear the filter). No-op when already empty."""
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self.emit(self.Callbacks.OnSelectionChanged)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_tree(self, topics: list[Topic]) -> None:
        """Build the roots → children structure + the id adjacency from a flat topic list. Roots and
        each child list are name-ordered for a stable display."""
        by_parent: dict[int | None, list[Topic]] = {}
        for t in topics:
            by_parent.setdefault(t.parent_id, []).append(t)
        for siblings in by_parent.values():
            siblings.sort(key=lambda t: t.name.lower())

        self._child_ids = {t.id: [c.id for c in by_parent.get(t.id, [])] for t in topics}

        def build(topic: Topic) -> TopicNode:
            return TopicNode(topic=topic, children=[build(c) for c in by_parent.get(topic.id, [])])

        self._roots = [build(t) for t in by_parent.get(None, [])]

    def _subtree_ids(self, topic_id: int) -> set[int]:
        """``topic_id`` plus every descendant id, via an in-memory walk of the adjacency map. Empty
        if the id isn't in the tree."""
        if topic_id not in self._child_ids:
            return set()
        out: set[int] = set()
        stack = [topic_id]
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)
            stack.extend(self._child_ids.get(nid, ()))
        return out
