"""A minimal, general-purpose directed graph.

Design notes
------------
The central decision: the graph stores *structure only*. Nodes are arbitrary user objects that carry
their own state, and the only requirement placed on them is hashability -- which every plain Python
class already satisfies via identity. This means "implements Node" reduces to "is hashable", so any of
these work out of the box::

    class State:                      # identity-hashed, mutable state: fine
        def __init__(self, name): self.name, self.visits = name, 0

    @dataclass(frozen=True)
    class Cell:                       # value-hashed: also fine
        x: int
        y: int

    g: Graph[State] = Graph()

Structure and state stay decoupled: you can mutate a node's payload freely without ever touching the
graph, and the same node objects can participate in several graphs at once.

The graph is directed, undirected behaviour is two add_edge calls away, and parallel edges are not
modelled (an edge either exists or it doesn't). Self-loops are allowed.

Adjacency is insertion-ordered: ``successors`` returns children in edge-creation order and
``predecessors`` returns parents in arrival order, making every traversal deterministic. Consumers
may lean on this — sibling order in a conversation graph is creation order, a resource's sections
keep document order — and re-adding an existing edge keeps its original position.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Iterable, Iterator
from typing import Generic, TypeVar

N = TypeVar("N", bound=Hashable)

__all__ = ["Graph", "CycleError"]


class CycleError(ValueError):
    """Raised when an operation that requires a DAG meets a cycle."""


class Graph(Generic[N]):
    """A directed graph over arbitrary hashable node objects of type ``N``.

    >>> g = Graph([("a", "b"), ("b", "c"), ("a", "c")])
    >>> g.successors("a")
    ('b', 'c')
    >>> g.topological_order()
    ['a', 'b', 'c']
    >>> list(g.dfs("a"))[0]
    'a'
    """

    __slots__ = ("_out", "_in")

    def __init__(
        self,
        edges: Iterable[tuple[N, N]] = (),
        nodes: Iterable[N] = (),
    ) -> None:
        # Both maps always share an identical key set: every node in the graph, in insertion order.
        # Values are insertion-ordered adjacency sets (dicts with None values): O(1) membership and
        # removal, with deterministic iteration order preserved.
        self._out: dict[N, dict[N, None]] = {}
        self._in: dict[N, dict[N, None]] = {}
        for node in nodes:
            self.add(node)
        for src, dst in edges:
            self.add_edge(src, dst)

    # -- construction ----------------------------------------------------

    def add(self, node: N) -> N:
        """Add ``node`` (a no-op if present). Returns the node for chaining."""
        if node not in self._out:
            self._out[node] = {}
            self._in[node] = {}
        return node

    def add_edge(self, src: N, dst: N) -> None:
        """Add the edge ``src -> dst``, adding either endpoint if missing. Re-adding an existing
        edge is a no-op that keeps the edge's original position in the adjacency order."""
        self.add(src)
        self.add(dst)
        self._out[src][dst] = None
        self._in[dst][src] = None

    def remove(self, node: N) -> None:
        """Remove ``node`` and every edge incident to it."""
        if node not in self._out:
            raise KeyError(node)
        preds = self._in.pop(node)
        succs = self._out.pop(node)
        for p in preds:
            if p != node:
                self._out[p].pop(node, None)
        for s in succs:
            if s != node:
                self._in[s].pop(node, None)

    def remove_edge(self, src: N, dst: N) -> None:
        """Remove the edge ``src -> dst`` (the nodes themselves remain)."""
        try:
            del self._out[src][dst]
            del self._in[dst][src]
        except KeyError:
            raise KeyError(f"no edge {src!r} -> {dst!r}") from None

    # -- queries -----------------------------------------------------------

    def __contains__(self, node: object) -> bool:
        return node in self._out

    def __len__(self) -> int:
        return len(self._out)

    def __iter__(self) -> Iterator[N]:
        """Iterate over nodes in insertion order."""
        return iter(self._out)

    def edges(self) -> Iterator[tuple[N, N]]:
        """Iterate over all edges as ``(src, dst)`` pairs."""
        for src, succs in self._out.items():
            for dst in succs:
                yield (src, dst)

    def has_edge(self, src: N, dst: N) -> bool:
        succs = self._out.get(src)
        return succs is not None and dst in succs

    def successors(self, node: N) -> tuple[N, ...]:
        """Children of ``node``, in edge-creation order."""
        return tuple(self._out[node])

    def predecessors(self, node: N) -> tuple[N, ...]:
        """Parents of ``node``, in edge-arrival order (first parent = first edge added)."""
        return tuple(self._in[node])

    def out_degree(self, node: N) -> int:
        return len(self._out[node])

    def in_degree(self, node: N) -> int:
        return len(self._in[node])

    def sources(self) -> Iterator[N]:
        """Nodes with no incoming edges."""
        return (n for n in self._out if not self._in[n])

    def sinks(self) -> Iterator[N]:
        """Nodes with no outgoing edges."""
        return (n for n in self._out if not self._out[n])

    # -- traversal -----------------------------------------------------------

    def dfs(self, start: N) -> Iterator[N]:
        """Depth-first preorder from ``start`` (lazy)."""
        if start not in self._out:
            raise KeyError(start)
        seen: set[N] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            yield node
            stack.extend(s for s in self._out[node] if s not in seen)

    def bfs(self, start: N) -> Iterator[N]:
        """Breadth-first order from ``start`` (lazy)."""
        if start not in self._out:
            raise KeyError(start)
        seen = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            yield node
            for s in self._out[node]:
                if s not in seen:
                    seen.add(s)
                    queue.append(s)

    def topological_order(self) -> list[N]:
        """All nodes in a topological order (Kahn's algorithm).

        Raises :class:`CycleError` if the graph contains a cycle.
        """
        indegree = {n: len(self._in[n]) for n in self._out}
        ready = deque(n for n, d in indegree.items() if d == 0)
        order: list[N] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for s in self._out[node]:
                indegree[s] -= 1
                if indegree[s] == 0:
                    ready.append(s)
        if len(order) != len(indegree):
            raise CycleError("graph contains a cycle")
        return order

    def is_acyclic(self) -> bool:
        try:
            self.topological_order()
        except CycleError:
            return False
        return True

    # -- derived graphs --------------------------------------------------------

    def reverse(self) -> "Graph[N]":
        """A new graph with every edge flipped (the transpose)."""
        g: Graph[N] = Graph(nodes=self)
        for src, dst in self.edges():
            g.add_edge(dst, src)
        return g

    def subgraph(self, nodes: Iterable[N]) -> "Graph[N]":
        """The induced subgraph on ``nodes`` (edges with both ends kept)."""
        keep = set(nodes)
        g: Graph[N] = Graph(nodes=(n for n in self if n in keep))
        for src, dst in self.edges():
            if src in keep and dst in keep:
                g.add_edge(src, dst)
        return g

    def copy(self) -> "Graph[N]":
        """A structural copy (node objects themselves are shared, not cloned)."""
        return self.subgraph(self)

    # -- misc ------------------------------------------------------------------

    def __repr__(self) -> str:
        n_edges = sum(len(s) for s in self._out.values())
        return f"<Graph: {len(self)} nodes, {n_edges} edges>"