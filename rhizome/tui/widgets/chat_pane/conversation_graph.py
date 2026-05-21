"""ConversationGraph — DAG of branch nodes with per-node feeds and a cursor path.

Each node owns a sequential ``feed`` of opaque ``Item`` s and an ``is_open`` flag.  Directed edges
encode causal order: every parent stores its children in left-to-right horizontal order, and every
child stores its parents in arrival order (leftmost parent = the "home" branch a merge was launched
from).  ``ConversationGraphCursor`` is an immutable root-to-node path through the DAG; the visible feed is the
concatenation of node feeds along that path.

The data model:

- A fresh graph has a single open root node and ``cursor_at_root()`` selects it.
- ``branch(cursor)`` closes the cursor's leaf and opens two new children: a *continuation* (leftmost,
  inherits the parent's name) and a fresh *branch* (rightmost).  The returned cursor descends into
  the continuation.
- ``merge(cursor, away_leaf)`` closes both leaves and opens a new node whose parents are
  ``(cursor.head, away_leaf)`` in that order; the returned cursor descends into the merged node.
- ``append(cursor, item)`` appends to the leaf node's feed and requires the leaf to be open.

The path-as-cursor representation is what makes merge-aware indicator visibility fall out naturally:
a branch point at node ``X`` is visible iff ``X`` appears on the cursor path, so two cursors that
arrive at the same merge node via different parents see different sets of upstream branch points.

The graph is purely structural — it knows nothing of agent sessions, widgets, message rendering,
shared state, or resources.  Consumers compose these around it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


NodeId = int


@dataclass
class ConversationNode[Item]:
    """A single segment of conversation between branch/merge boundaries.

    The ``feed`` accumulates items until the node is closed (by ``branch`` or ``merge``).  ``name``
    is an optional human label; the continuation child of a ``branch`` inherits the parent's name
    by default so that the "main line" preserves its identity across branch points.

    All fields here MUST keep their defaults — consumers subclass this to attach domain-specific
    per-branch state (passed to ``ConversationGraph`` via ``node_cls``), and dataclass inheritance
    requires every parent field to have a default if any subclass field does.
    """

    id: NodeId
    feed: list[Item] = field(default_factory=list)
    is_open: bool = True
    name: str | None = None


@dataclass(frozen=True)
class ConversationGraphCursor:
    """Immutable root-to-node path through a ConversationGraph.

    The path is a sequence of NodeIds where each consecutive pair must form a parent→child edge in
    the graph.  ``head`` is the currently-selected node; it need not be a leaf, but consumers will
    typically treat non-leaf cursors as a navigational waypoint rather than a write target.
    """

    path: tuple[NodeId, ...]

    @property
    def head(self) -> NodeId:
        return self.path[-1]

    def __len__(self) -> int:
        return len(self.path)

    def __iter__(self) -> Iterator[NodeId]:
        return iter(self.path)


class ConversationGraph[Item]:
    """DAG of conversation branch nodes with cursor-based navigation.

    The graph is append-only with respect to topology: nodes are never removed and edges are never
    rewritten.  Node payload (``feed``, ``name``) and ``is_open`` may change.  All structural
    mutations go through ``branch`` and ``merge``, both of which preserve the invariant that every
    non-root node has at least one parent and every edge is recorded in both ``_children`` and
    ``_parents``.
    """

    def __init__(
        self,
        root_name: str | None = "main",
        *,
        node_cls: type[ConversationNode[Item]] = ConversationNode,
    ) -> None:
        """Consumers can pass ``node_cls`` to attach domain-specific per-branch state to each node.

        The subclass must be default-initializable from ``(id=..., name=...)`` — i.e. every field
        introduced by the subclass needs a default value. The graph never inspects or constructs
        subclass-specific fields; it only stores nodes by id.
        """
        self._node_cls = node_cls
        self._nodes: dict[NodeId, ConversationNode[Item]] = {}
        self._children: dict[NodeId, list[NodeId]] = {}
        self._parents: dict[NodeId, list[NodeId]] = {}
        self._next_id: int = 0
        self._root_id = self._new_node(name=root_name)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def root(self) -> NodeId:
        return self._root_id

    def node(self, node_id: NodeId) -> ConversationNode[Item]:
        return self._nodes[node_id]

    def children(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """Children of ``node_id`` in left-to-right horizontal order."""
        return tuple(self._children[node_id])

    def parents(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """Parents of ``node_id`` in arrival order (leftmost = home branch for merges)."""
        return tuple(self._parents[node_id])

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def __iter__(self) -> Iterator[NodeId]:
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    # ------------------------------------------------------------------
    # Mutations: branch, merge, append, rename
    # ------------------------------------------------------------------

    def append(self, cursor: ConversationGraphCursor, item: Item) -> None:
        """Append ``item`` to the feed of the cursor's leaf.  Leaf must be open."""
        node = self._nodes[cursor.head]
        if not node.is_open:
            raise ValueError(f"Cannot append to closed node {cursor.head}")
        node.feed.append(item)

    def branch(
        self,
        cursor: ConversationGraphCursor,
        *,
        continuation_name: str | None = None,
        branch_name: str | None = None,
    ) -> tuple[ConversationGraphCursor, NodeId]:
        """Add a new branch child to the cursor's node and descend into it.

        Two cases, unified under one method:

        - **First branch from an open leaf**: closes the parent and adds *two* children — a
          continuation (leftmost, inheriting the parent's name by default) and the new branch
          (rightmost). The cursor descends into the new branch.
        - **Subsequent branch from a closed parent that already has children**: just adds
          another child to the right of the existing ones. No continuation is created (the
          parent already paid that cost on the first branch). The cursor descends into the
          new branch.

        ``continuation_name`` only applies to the first-branch case; passing it when the parent
        is already closed is rejected to surface caller confusion.

        Returns ``(descended_cursor, new_branch_id)``.
        """
        parent_id = cursor.head
        parent = self._nodes[parent_id]

        if parent.is_open:
            # First-branch case: close the parent and create the continuation.
            parent.is_open = False
            cont_id = self._new_node(
                name=continuation_name if continuation_name is not None else parent.name
            )
            self._add_edge(parent_id, cont_id)
        elif continuation_name is not None:
            raise ValueError(
                f"continuation_name only applies to the first branch from a node; "
                f"node {parent_id} already has children",
            )

        new_id = self._new_node(name=branch_name)
        self._add_edge(parent_id, new_id)

        return ConversationGraphCursor(cursor.path + (new_id,)), new_id

    def merge(
        self,
        cursor: ConversationGraphCursor,
        away_leaf: NodeId,
        *,
        merged_name: str | None = None,
    ) -> ConversationGraphCursor:
        """Merge ``away_leaf`` into the cursor's leaf.

        Closes both source leaves and opens a new node whose parents are ``(cursor.head, away_leaf)``
        in that order.  The returned cursor descends into the merged node along the home (left)
        parent path; use ``swap_merge_parent`` afterwards to view the same merged node via the away
        parent's path.
        """
        home_id = cursor.head
        if home_id == away_leaf:
            raise ValueError("Cannot merge a node with itself")
        home = self._nodes[home_id]
        away = self._nodes[away_leaf]
        if not home.is_open:
            raise ValueError(f"Cannot merge: home leaf {home_id} is closed")
        if not away.is_open:
            raise ValueError(f"Cannot merge: away leaf {away_leaf} is closed")
        if self._children[home_id] or self._children[away_leaf]:
            raise ValueError("Cannot merge: a source leaf has children")

        home.is_open = False
        away.is_open = False

        merged_id = self._new_node(name=merged_name if merged_name is not None else home.name)
        self._add_edge(home_id, merged_id)
        self._add_edge(away_leaf, merged_id)

        return ConversationGraphCursor(cursor.path + (merged_id,))

    def rename(self, node_id: NodeId, name: str | None) -> None:
        self._nodes[node_id].name = name

    # ------------------------------------------------------------------
    # ConversationGraphCursor navigation
    # ------------------------------------------------------------------

    def cursor_at_root(self) -> ConversationGraphCursor:
        return ConversationGraphCursor((self._root_id,))

    def is_valid_cursor(self, cursor: ConversationGraphCursor) -> bool:
        """True iff ``cursor.path`` is a non-empty root-to-node walk along real edges."""
        if not cursor.path or cursor.path[0] != self._root_id:
            return False
        for parent, child in zip(cursor.path, cursor.path[1:]):
            if child not in self._children.get(parent, ()):
                return False
        return True

    def ascend(self, cursor: ConversationGraphCursor) -> ConversationGraphCursor:
        """Pop the leaf, leaving the cursor at the parent waypoint.  Raises at the root."""
        if len(cursor) < 2:
            raise ValueError("Cannot ascend: cursor is at the root")
        return ConversationGraphCursor(cursor.path[:-1])

    def descend(self, cursor: ConversationGraphCursor, child_id: NodeId) -> ConversationGraphCursor:
        """Extend the cursor by descending into one of the leaf's children."""
        if child_id not in self._children[cursor.head]:
            raise ValueError(f"{child_id} is not a child of {cursor.head}")
        return ConversationGraphCursor(cursor.path + (child_id,))

    def sibling(self, cursor: ConversationGraphCursor, direction: int) -> ConversationGraphCursor:
        """Move horizontally among the leaf's siblings.  ``direction`` is +1 (right) or -1 (left)."""
        if direction not in (-1, 1):
            raise ValueError("direction must be -1 or +1")
        if len(cursor) < 2:
            raise ValueError("Cannot move sibling at the root: no parent to share")
        parent_id = cursor.path[-2]
        siblings = self._children[parent_id]
        idx = siblings.index(cursor.head)
        new_idx = idx + direction
        if not 0 <= new_idx < len(siblings):
            raise ValueError("No sibling in that direction")
        return ConversationGraphCursor(cursor.path[:-1] + (siblings[new_idx],))

    def swap_merge_parent(self, cursor: ConversationGraphCursor, parent_idx: int) -> ConversationGraphCursor:
        """Rebuild the cursor to arrive at the same merge node via a different parent.

        ``parent_idx`` indexes the merge node's parent list (0 = home, 1+ = merged-in branches).
        The prefix is regenerated as the *canonical* path from root to the chosen parent — walking
        leftmost parent at each step when ambiguity arises (i.e. nested merges in the prefix).  The
        cursor's leaf is preserved.
        """
        head = cursor.head
        ps = self._parents[head]
        if len(ps) < 2:
            raise ValueError(f"Node {head} is not a merge node")
        if not 0 <= parent_idx < len(ps):
            raise ValueError(f"parent_idx {parent_idx} out of range")
        new_prefix = self._canonical_path_to(ps[parent_idx])
        return ConversationGraphCursor(tuple(new_prefix) + (head,))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def visible_feed(self, cursor: ConversationGraphCursor) -> list[Item]:
        """Concatenated feeds of all nodes on the cursor path, in path order."""
        out: list[Item] = []
        for nid in cursor.path:
            out.extend(self._nodes[nid].feed)
        return out

    def feed_segments(self, cursor: ConversationGraphCursor) -> list[tuple[NodeId, list[Item]]]:
        """Per-node ``(id, feed-snapshot)`` pairs along the cursor path.

        Snapshots are shallow copies so callers cannot mutate node-owned state by accident.  Use
        this when the view needs to know which node each segment belongs to — e.g. to insert
        branch/merge boundary indicators between segments.
        """
        return [(nid, list(self._nodes[nid].feed)) for nid in cursor.path]

    def branch_points_on(self, cursor: ConversationGraphCursor) -> list[NodeId]:
        """Nodes on the cursor path with more than one child (branch points)."""
        return [nid for nid in cursor.path if len(self._children[nid]) > 1]

    def merge_points_on(self, cursor: ConversationGraphCursor) -> list[NodeId]:
        """Nodes on the cursor path with more than one parent (merge nodes)."""
        return [nid for nid in cursor.path if len(self._parents[nid]) > 1]

    def open_leaves(self) -> list[NodeId]:
        """Open nodes with no children — i.e. branches still accepting new items."""
        return [nid for nid, node in self._nodes.items() if node.is_open and not self._children[nid]]

    def canonical_path_to(self, node_id: NodeId) -> ConversationGraphCursor:
        """ConversationGraphCursor that reaches ``node_id`` via leftmost-parent-at-each-step from the root."""
        return ConversationGraphCursor(tuple(self._canonical_path_to(node_id)))

    def find_by_name(self, name: str) -> list[NodeId]:
        """All nodes carrying ``name``.  Names are not unique; the caller decides disambiguation."""
        return [nid for nid, node in self._nodes.items() if node.name == name]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _new_node(self, *, name: str | None) -> NodeId:
        nid = self._next_id
        self._next_id += 1
        self._nodes[nid] = self._node_cls(id=nid, name=name)
        self._children[nid] = []
        self._parents[nid] = []
        return nid

    def _add_edge(self, parent: NodeId, child: NodeId) -> None:
        self._children[parent].append(child)
        self._parents[child].append(parent)

    def _canonical_path_to(self, target: NodeId) -> list[NodeId]:
        """Walk backward from ``target`` via leftmost parent at each step until the root."""
        if target not in self._nodes:
            raise ValueError(f"Unknown node {target}")
        rev: list[NodeId] = [target]
        cur = target
        while cur != self._root_id:
            ps = self._parents[cur]
            if not ps:
                raise RuntimeError(f"Non-root node {cur} has no parents — graph is corrupt")
            cur = ps[0]
            rev.append(cur)
        rev.reverse()
        return rev
