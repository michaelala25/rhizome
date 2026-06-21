"""A rooted "tree with merges" built on top of ``graph.Graph``.

The structure
-------------
Start with a rooted tree and allow a node to acquire more than one parent -- a *merge*, expressed
directly with ``add_edge``. The result is a rooted DAG, but the tree you usually care about is the
*unfolding* of that DAG: the tree whose nodes are root-to-X **paths**. With C a child of both B and D,
the DAG has one node C, while the unfolding has two distinct locations, ``A -> B -> C`` and
``A -> D -> C``.

Consequently this module's central type is :class:`Path`: an immutable, hashable value identifying one
location in the unfolding. State splits naturally in two:

- **per-node state** lives in your node objects, shared by every path through the node (that is what
  merging *means*);
- **per-path state** lives outside, keyed by ``Path`` -- e.g. ``visited: dict[Path[N], bool]``.

:class:`MergeTree` wraps a :class:`graph.Graph` (composition, not inheritance) and narrows its mutation
API to preserve two invariants:

1. there is a single distinguished root with no incoming edges;
2. the graph is acyclic, always -- every ``add_edge`` is validated before it lands.

Because acyclicity is enforced, traversals over the unfolding need no ``seen`` set -- a merged node is
deliberately visited once per path. Beware: stacked merges can make the number of root-to-leaf paths
grow exponentially, so every traversal here is a lazy generator and ``Path`` extension is O(1) via
structural sharing.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator
from typing import Generic, Optional, TypeVar

from .graph import CycleError, Graph

N = TypeVar("N", bound=Hashable)

__all__ = ["Path", "MergeTree", "CycleError"]


class Path(Generic[N]):
    """An immutable root-to-node path; one location in the unfolded tree.

    Implemented as a cons cell -- ``node`` plus a ``parent`` prefix path -- so extending a path is O(1)
    and prefixes are shared between siblings. Paths are hashable and compare by node sequence, making
    them usable as dictionary keys for per-path state.

    A ``Path`` is a pure value: it does not know about any graph and is not validated on construction.
    Use :meth:`MergeTree.extend` to build paths that are checked against actual edges, and
    :meth:`MergeTree.is_valid` to re-check a stored path after mutations.
    """

    __slots__ = ("node", "parent", "_length")

    def __init__(self, node: N, parent: Optional["Path[N]"] = None) -> None:
        self.node = node          # last node of this path
        self.parent = parent      # prefix path, or None if this is the root
        self._length = 1 if parent is None else len(parent) + 1

    def nodes(self) -> tuple[N, ...]:
        """The full node sequence, root first."""
        out: list[N] = []
        step: Optional[Path[N]] = self
        while step is not None:
            out.append(step.node)
            step = step.parent
        out.reverse()
        return tuple(out)

    def __iter__(self) -> Iterator[N]:
        return iter(self.nodes())

    def __len__(self) -> int:
        return self._length

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Path):
            return NotImplemented
        if self._length != other._length:
            return False
        return self.nodes() == other.nodes()

    def __hash__(self) -> int:
        return hash(self.nodes())

    def __repr__(self) -> str:
        return f"Path({' -> '.join(repr(n) for n in self)})"


class MergeTree(Generic[N]):
    """A rooted, always-acyclic DAG addressed by :class:`Path` values.

    >>> t = MergeTree("A")
    >>> for parent, child in [("A","B"), ("A","D"), ("B","C"), ("D","C")]:
    ...     t.add_edge(parent, child)
    >>> sorted(str(p) for p in t.paths_to("C"))
    ["Path('A' -> 'B' -> 'C')", "Path('A' -> 'D' -> 'C')"]

    Sibling and parent order are inherited from the underlying graph's insertion-ordered adjacency:
    ``children`` yields siblings in creation order, ``parents`` in arrival order, and the path-level
    traversals (``walk``, ``leaf_paths``) visit subtrees in that same left-to-right order.
    """

    __slots__ = ("_g", "_root")

    def __init__(self, root: N) -> None:
        self._g: Graph[N] = Graph(nodes=(root,))
        self._root = root

    # -- structure -----------------------------------------------------------

    @property
    def root(self) -> N:
        return self._root

    @property
    def graph(self) -> Graph[N]:
        """The underlying quotient DAG. Treat as read-only: mutate it only through ``MergeTree``, which
        maintains the invariants. Useful for reusing node-level algorithms (``topological_order`` etc.)."""
        return self._g

    def add_edge(self, parent: N, child: N) -> None:
        """Add ``parent -> child``; ``child`` is created if new.

        Raises ``KeyError`` if ``parent`` is unknown, ``ValueError`` if ``child`` is the root, and
        :class:`CycleError` if the edge would create a cycle.
        """
        if parent not in self._g:
            raise KeyError(parent)
        if child == self._root:
            raise ValueError("the root cannot acquire a parent")
        if child in self._g and self.reachable(child, parent):
            raise CycleError(f"edge {parent!r} -> {child!r} would create a cycle")
        self._g.add_edge(parent, child)

    def remove_edge(self, parent: N, child: N) -> None:
        """Remove an edge. May leave nodes unreachable from the root; such nodes simply stop appearing
        in any path (see :meth:`prune`)."""
        self._g.remove_edge(parent, child)

    def remove(self, node: N) -> None:
        """Remove a node and its incident edges. The root cannot be removed."""
        if node == self._root:
            raise ValueError("cannot remove the root")
        self._g.remove(node)

    def prune(self) -> set[N]:
        """Remove every node unreachable from the root; returns the removed set."""
        reachable = set(self._g.dfs(self._root))
        dead = {n for n in self._g if n not in reachable}
        for n in dead:
            self._g.remove(n)
        return dead

    # -- node-level queries (the quotient DAG) ------------------------------

    def children(self, node: N) -> tuple[N, ...]:
        """Children in edge-creation order — sibling order is the order branches were made."""
        return self._g.successors(node)

    def parents(self, node: N) -> tuple[N, ...]:
        """Parents in arrival order — for merge nodes, the first parent is the "home" lineage."""
        return self._g.predecessors(node)

    def reachable(self, src: N, dst: N) -> bool:
        """Whether ``dst`` can be reached from ``src`` along edges. Reflexive: a node reaches itself."""
        return any(n == dst for n in self._g.dfs(src))

    def __contains__(self, node: object) -> bool:
        return node in self._g

    # -- path-level interface (the unfolding) -------------------------------

    def root_path(self) -> Path[N]:
        return Path(self._root)

    def extend(self, path: Path[N], child: N) -> Path[N]:
        """Extend a path by one edge, validating that the edge exists."""
        if not self._g.has_edge(path.node, child):
            raise KeyError(f"no edge {path.node!r} -> {child!r}")
        return Path(child, path)

    def step(self, path: Path[N]) -> Iterator[Path[N]]:
        """All one-edge extensions of ``path`` (its children in the unfolding)."""
        for child in self._g.successors(path.node):
            yield Path(child, path)

    def walk(self) -> Iterator[Path[N]]:
        """Depth-first preorder over the *unfolding*: every root-to-X path.

        Note the deliberate absence of a ``seen`` set -- a merged node is yielded once per distinct path
        to it. Termination is guaranteed by the acyclicity invariant.
        """
        stack: list[Path[N]] = [self.root_path()]
        while stack:
            path = stack.pop()
            yield path
            stack.extend(self.step(path))

    def leaf_paths(self) -> Iterator[Path[N]]:
        """Every complete root-to-leaf path."""
        return (p for p in self.walk() if not self._g.successors(p.node))

    def paths_to(self, node: N) -> Iterator[Path[N]]:
        """Every root-to-``node`` path (lazily, exploring only ancestors)."""
        if node not in self._g:
            raise KeyError(node)

        def ascend(n: N) -> Iterator[Path[N]]:
            if n == self._root:
                yield Path(n)
                return
            for parent in self._g.predecessors(n):
                for prefix in ascend(parent):
                    yield Path(n, prefix)

        return ascend(node)

    def is_valid(self, path: Path[N]) -> bool:
        """Whether ``path`` still starts at the root and follows real edges.

        Paths are immutable snapshots; after ``remove_edge`` / ``remove``, previously obtained paths
        may go stale.
        """
        seq = path.nodes()
        if seq[0] != self._root:
            return False
        return all(self._g.has_edge(a, b) for a, b in zip(seq, seq[1:]))

    # -- internals -----------------------------------------------------------

    def __repr__(self) -> str:
        return f"<MergeTree rooted at {self._root!r}: {self._g!r}>"