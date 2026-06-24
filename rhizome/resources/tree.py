"""The resource/section hierarchy as a content-free skeleton.

``ResourceTree`` answers parentage/coverage questions for the resource stores and the viewer's display
tree: ids and structure only — content NEVER loads through this object. It is built eagerly (the full
skeleton is two column-only queries, milliseconds at any realistic library size) and owned once per
workspace, shared by every store; ``refresh()`` re-pulls it after resource/section CRUD. Keeping the
skeleton in memory is what lets store arithmetic stay synchronous and pure on the keystroke path.

Shape: resources are roots; a resource's top-level sections (``parent_id`` NULL) hang off it; nested
sections hang off their parent section.
"""

from dataclasses import dataclass
from typing import Iterable, Literal

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rhizome.db.operations.resources import fetch_resource_skeleton
from rhizome.utils.data_structures import Graph


@dataclass(frozen=True)
class ResourceTreeNode:
    kind: Literal["resource", "section"]
    id: int


class ResourceTree:
    """One workspace's skeleton.

    Rebuilt atomically by ``load_rows`` (which ``refresh`` feeds from the DB): readers holding a
    reference always see either the old snapshot or the new one, never a half-built graph.
    """

    def __init__(self, session_factory: "async_sessionmaker[AsyncSession] | None" = None) -> None:
        self._session_factory = session_factory
        self._graph: Graph[ResourceTreeNode] = Graph()
        self._roots: tuple[ResourceTreeNode, ...] = ()

    @property
    def roots(self) -> tuple[ResourceTreeNode, ...]:
        return self._roots

    def __contains__(self, node: object) -> bool:
        return node in self._graph

    def __len__(self) -> int:
        return len(self._graph)

    def parent(self, node: ResourceTreeNode) -> ResourceTreeNode | None:
        """The node's parent, or ``None`` for roots and ids no longer in the tree."""
        if node not in self._graph:
            return None
        return next(iter(self._graph.predecessors(node)), None)

    def children(self, node: ResourceTreeNode) -> tuple[ResourceTreeNode, ...]:
        """The node's children; empty for leaves and for ids no longer in the tree."""
        if node not in self._graph:
            return ()
        return self._graph.successors(node)

    def load_rows(
        self,
        resource_ids: Iterable[int],
        section_rows: Iterable[tuple[int, int, int | None]],
    ) -> None:
        """Rebuild the skeleton from raw rows — ``refresh`` fetches them; tests supply them directly.

        Row order is irrelevant: ``Graph.add_edge`` creates endpoints on demand, so a child section
        may arrive before its parent.
        """
        graph: Graph[ResourceTreeNode] = Graph()
        roots = tuple(graph.add(ResourceTreeNode("resource", rid)) for rid in resource_ids)

        for section_id, resource_id, parent_id in section_rows:
            parent = (
                ResourceTreeNode("section", parent_id) if parent_id is not None
                else ResourceTreeNode("resource", resource_id)
            )
            graph.add_edge(parent, ResourceTreeNode("section", section_id))

        # Atomic swap — see class docstring.
        self._graph, self._roots = graph, roots

    async def refresh(self) -> None:
        """Re-pull the skeleton from the DB. Call after resource/section CRUD; stores referencing
        ids that vanished degrade gracefully (see ``store.py``), but a ``store.prune()`` sweep after
        refreshing keeps their descriptions tidy."""
        if self._session_factory is None:
            raise RuntimeError("ResourceTree was constructed without a session factory")
        async with self._session_factory() as session:
            resource_ids, section_rows = await fetch_resource_skeleton(session)
        self.load_rows(resource_ids, section_rows)
