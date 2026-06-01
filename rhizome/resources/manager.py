"""ResourceManager — tracks loaded resource state and produces messages for the agent session.

State is represented in **minimum-description-length (MDL) form**: a flat
``dict[ResourceTreeNodeKey, ResourceLoadType]`` where an entry at node *X* means "*X* and every
descendant of *X* are loaded at this mode, unless a descendant has its own
entry overriding it."

The loader pushes full snapshots via :meth:`set_state` on every user toggle.
The agent session calls :meth:`consume` at the start of each stream, which
diffs the current snapshot against the last-consumed snapshot and returns a
list of messages to inject into the graph state:

- For every resource whose context-stuffed (CS) entry set changed, emit one
  :class:`HumanMessage` with deterministic id
  ``rhizome-resource-ctx-{resource_id}``.  The ``add_messages`` reducer
  replaces prior messages with the same id in place, so toggling CS'd
  sections updates content without appending duplicates.
- For every resource that had CS content before and has none now, emit a
  :class:`RemoveMessage` with the same id to drop it from graph state.
- Whenever the INDEX scope changes, emit a single vector-store digest
  :class:`HumanMessage` (id ``rhizome-vector-store-digest``) listing what's
  currently queryable via the ``query_resources`` tool, or a
  :class:`RemoveMessage` with that id when the INDEX scope becomes empty.

Owner resolution (which resource a section belongs to) is done on demand
via a single batched DB lookup at the top of :meth:`consume`; sections are
treated as static post-creation, so no cache is needed.
"""

from __future__ import annotations

from collections import defaultdict
import enum
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage

from sqlalchemy.ext.asyncio import AsyncSession

from rhizome.db.models import Resource, ResourceSection
from rhizome.db.operations import (
    get_chunks,
    get_chunks_for_section,
    get_resource_with_content_and_sections,
    get_section_resource_ids,
)
from rhizome.logs import get_logger
from rhizome.resources.context_message import (
    build_resource_context_message,
    resource_context_message_id,
)
from rhizome.resources.embeddings import compute_embeddings, has_embeddings
from rhizome.resources.vector_store import ChunkMeta, ResourceVectorStore

_log = get_logger("resources.manager")


# ---------------------------------------------------------------------------
# State representation
# ---------------------------------------------------------------------------

class ResourceLoadType(enum.Enum):
    """How a resource or section is made available to the agent.

    - ``INDEX`` — embedded into the vector store and retrievable via the ``query_resources`` tool.
    - ``CONTEXT`` — stuffed verbatim into the agent's context window.
    """

    INDEX = "index"
    CONTEXT = "context"


ResourceTreeNodeKind = Literal["resource", "section"]
ResourceTreeNodeKey = tuple[ResourceTreeNodeKind, int]


# Deterministic id for the vector-store digest message so the add_messages
# reducer can replace (or remove) a prior digest in place on every toggle.
VECTOR_STORE_DIGEST_MESSAGE_ID = "rhizome-vector-store-digest"


def _fmt_state(state: dict[ResourceTreeNodeKey, ResourceLoadType]) -> str:
    if not state:
        return "(empty)"
    parts = [f"{kind[0]}{nid}:{mode.value}" for (kind, nid), mode in sorted(state.items())]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Tracks MDL load state and produces context-stuffing messages."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory

        self._current: dict[ResourceTreeNodeKey, ResourceLoadType] = {}
        self._next: dict[ResourceTreeNodeKey, ResourceLoadType] = {}

        self._vector_store = ResourceVectorStore()
        self._embedding_in_progress: set[int] = set()


    @property
    def vector_store(self) -> ResourceVectorStore:
        """FAISS-backed store over the currently-indexed chunks.

        Rebuilt inside :meth:`consume` whenever the set of INDEX MDL entries
        changes.  Agent tools close over this reference to query retrieval.
        """
        return self._vector_store

    # ------------------------------------------------------------------
    # State updates (called by the UI layer)
    # ------------------------------------------------------------------

    def set_state(self, state: dict[ResourceTreeNodeKey, ResourceLoadType]) -> None:
        """Replace the next state wholesale with a snapshot from the loader."""
        new_next = dict(state)
        if new_next != self._next:
            self._next = new_next
            _log.debug("State updated: %s", _fmt_state(self._next))

    # ------------------------------------------------------------------
    # Embedding lifecycle
    # ------------------------------------------------------------------

    def is_embedding_in_progress(self, resource_id: int) -> bool:
        """True if an embedding computation is in-flight for this resource."""
        return resource_id in self._embedding_in_progress

    async def ensure_embedded(self, resource_id: int) -> bool:
        """Check for embeddings and compute them if missing.

        Returns ``True`` on success (embeddings now exist), ``False`` on
        failure (API error, missing raw_text, etc.).

        The caller is responsible for running this as an async task or
        Textual worker.
        """
        self._embedding_in_progress.add(resource_id)
        try:
            if await has_embeddings(self._session_factory, resource_id):
                _log.info("Resource %d already has embeddings", resource_id)
                return True

            _log.info("Computing embeddings for resource %d ...", resource_id)
            await compute_embeddings(self._session_factory, resource_id)
            _log.info("Embeddings complete for resource %d", resource_id)
            return True
        except Exception:
            _log.exception("Embedding failed for resource %d", resource_id)
            return False
        finally:
            self._embedding_in_progress.discard(resource_id)

    # ------------------------------------------------------------------
    # Consumption (called by AgentSession.stream)
    # ------------------------------------------------------------------

    async def _group_by_rid(self, nodes: set[ResourceTreeNodeKey]) -> dict[int, set[ResourceTreeNodeKey]]:
        by_rid: dict[int, set[ResourceTreeNodeKey]] = defaultdict(set)

        resources = set(node for node in nodes if node[0] == "resource")
        sections = set(node for node in nodes if node[0] == "section")

        # Group resource nodes by their own ID
        for node in resources:
            _, rid = node
            by_rid[rid].add(node)

        # Group section nodes by their owning resource ID, which requires a DB lookup
        section_ids = set(sid for _, sid in sections)
        async with self._session_factory() as session:
            section_rids = await get_section_resource_ids(session, section_ids)

        for node in sections:
            _, sid = node

            rid = section_rids.get(sid)
            if rid is None:
                _log.warning("Section %d has no resource owner; skipping", sid)
                continue

            by_rid[rid].add(node)

        return by_rid
    
    async def _build_vector_store(
        self,
        indexed_scope: set[ResourceTreeNodeKey],
    ) -> None:
        """Rebuild the vector store from the set of INDEX MDL nodes.

        If the loaded scope is empty, simply clear the store and return.

        Otherwise, we query resource_chunks for loaded resources and the
        resource_chunk_section merge table for which chunks belong to the
        specific sections scoped.  For chunks which belong to multiple
        loaded nodes (e.g. a resource and one of its sections, or two
        loaded sibling sections straddled by the same chunk), the winner
        for which node claims the breadcrumb is essentially arbitrary,
        determined by the order in which nodes appear when resolving the
        ``indexed_scope`` set.  No attempt is made to pin this down as it's
        rarely a big deal.
        """
        if not indexed_scope:
            await self._vector_store.clear()
            return

        entries: dict[int, tuple[ChunkMeta, bytes]] = {}
        async with self._session_factory() as session:
            for node in indexed_scope:
                for meta, emb in await self._build_resource_chunk_metas(session, node):
                    entries.setdefault(meta.chunk_id, (meta, emb))

        await self._vector_store.rebuild(list(entries.values()))

    async def _build_resource_chunk_metas(
        self,
        session: AsyncSession,
        node: ResourceTreeNodeKey,
    ) -> list[tuple[ChunkMeta, bytes]]:
        """Fetch embedded chunks for a single scoped node and hydrate metas.

        Resource nodes pull chunks via :func:`get_chunks`; section nodes go
        through :func:`get_chunks_for_section` (via the
        ``resource_chunk_section`` merge table).  Section nodes additionally
        walk the section's ancestor chain to build the breadcrumb string.
        """
        kind, nid = node
        if kind == "resource":
            resource = await session.get(Resource, nid)
            if resource is None:
                _log.warning("Resource %d not found while building vector store", nid)
                return []
            chunks = await get_chunks(session, nid, embedded_only=True)
            breadcrumb = ""
        else:  # "section"
            section = await session.get(ResourceSection, nid)
            if section is None:
                _log.warning("Section %d not found while building vector store", nid)
                return []
            resource = await session.get(Resource, section.resource_id)
            if resource is None:
                _log.warning(
                    "Resource %d not found for section %d", section.resource_id, nid,
                )
                return []
            chunks = await get_chunks_for_section(session, nid, embedded_only=True)
            breadcrumb = await _section_breadcrumb(session, section)

        return [
            (
                ChunkMeta(
                    chunk_id=chunk.id,
                    resource_id=resource.id,
                    resource_name=resource.name,
                    section_breadcrumb=breadcrumb,
                    context_tag=chunk.context_tag,
                ),
                chunk.embedding,
            )
            for chunk in chunks
        ]

    async def _build_vector_store_digest_message(
        self,
        indexed_scope: set[ResourceTreeNodeKey],
    ) -> BaseMessage:
        """Build a digest of what's queryable via the ``query_resources`` tool.

        Driven directly off the INDEX MDL scope (not the rebuilt store)
        since the scope is the canonical "what the user chose to load"
        view; the store is just its chunk-level materialization.  Returns
        a ``RemoveMessage`` when the scope is empty.
        """
        if not indexed_scope:
            return RemoveMessage(id=VECTOR_STORE_DIGEST_MESSAGE_ID)

        by_rid = await self._group_by_rid(indexed_scope)
        lines = ["Resources loaded:"]

        async with self._session_factory() as session:
            for rid in sorted(by_rid):
                nodes = by_rid[rid]
                resource = await session.get(Resource, rid)
                title = resource.name if resource is not None else "<unknown resource>"
                if ("resource", rid) in nodes:
                    lines.append(f"- {rid} {title} - fully loaded")
                else:
                    lines.append(f"- {rid} {title} - subsections:")
                    for sid in sorted(sid for kind, sid in nodes if kind == "section"):
                        section = await session.get(ResourceSection, sid)
                        sub_title = section.title if section is not None else "<unknown section>"
                        lines.append(f"  - {sid} {sub_title}")

        return HumanMessage(
            content="\n".join(lines),
            id=VECTOR_STORE_DIGEST_MESSAGE_ID,
        )

    async def _build_context_stuffed_messages(
        self,
        old_cs_by_rid: dict[int, set[ResourceTreeNodeKey]],
        new_cs_by_rid: dict[int, set[ResourceTreeNodeKey]],
    ):
        # First, determine which resources need rebuilds/removals
        removals: list[int] = []
        rebuilds: list[int] = []
        for rid in set(old_cs_by_rid) | set(new_cs_by_rid):
            old_entries = old_cs_by_rid.get(rid) or set()
            new_entries = new_cs_by_rid.get(rid) or set()

            # Identical content, skip
            if old_entries == new_entries:
                continue

            # Resource has been un-context-stuffed, queue for removal
            if not new_entries:
                removals.append(rid)
            else:
                # Resource has a new composition of context-stuffed subsections, queue for rebuild
                rebuilds.append(rid)

        messages: list[BaseMessage] = []
        
        for rid in sorted(removals):
            messages.append(RemoveMessage(id=resource_context_message_id(rid)))
        
        if not rebuilds:
            return messages
        
        if self._session_factory is None:
            _log.warning(
                "ResourceManager has no session_factory; skipping %d content fetch(es)",
                len(rebuilds),
            )
            return messages
        
        async with self._session_factory() as session:
            for rid in sorted(rebuilds):
                resource = await get_resource_with_content_and_sections(session, rid)

                if resource is None:
                    _log.warning(
                        "Resource %d not found while building context message", rid,
                    )
                    continue

                msg = build_resource_context_message(resource, new_cs_by_rid[rid])
                if msg is not None:
                    messages.append(msg)

        if messages:
            _log.info(
                "Constructed: %d msg(s) (%d rebuild, %d remove)",
                len(messages), len(rebuilds), len(removals),
            )

        return messages

    async def consume(self) -> list[BaseMessage]:
        """Diff ``_current`` vs ``_next`` and return messages for the graph.

        Emits one HumanMessage per resource whose CS entry set changed (new
        content or replacement of existing content), one RemoveMessage per
        resource that lost all its CS entries, and — whenever the INDEX
        scope changes — either a digest HumanMessage describing the new
        vector-store contents or a RemoveMessage dropping the prior digest.
        Advances ``_current`` to ``_next`` after producing the diff.
        """

        messages: list[BaseMessage] = []

        # First, rebuild the vector store and emit a digest summarizing its
        # contents so the agent knows what `query_resources` can retrieve.
        old_indexed: set[ResourceTreeNodeKey] = set(k for k, m in self._current.items() if m == ResourceLoadType.INDEX)
        new_indexed: set[ResourceTreeNodeKey] = set(k for k, m in self._next.items() if m == ResourceLoadType.INDEX)
        if old_indexed != new_indexed:
            await self._build_vector_store(new_indexed)
            messages.append(await self._build_vector_store_digest_message(new_indexed))

        # Second, compute message diff for context-stuffed resources/sections
        old_cs: set[ResourceTreeNodeKey] = set(k for k, m in self._current.items() if m == ResourceLoadType.CONTEXT)
        new_cs: set[ResourceTreeNodeKey] = set(k for k, m in self._next.items() if m == ResourceLoadType.CONTEXT)

        old_cs_by_rid = await self._group_by_rid(old_cs)
        new_cs_by_rid = await self._group_by_rid(new_cs)

        if old_cs_by_rid != new_cs_by_rid:
            messages.extend(await self._build_context_stuffed_messages(
                old_cs_by_rid,
                new_cs_by_rid,
            ))

        self._current = dict(self._next)

        if not messages:
            _log.debug("Consumed with no pending messages")

        return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _section_breadcrumb(
    session: AsyncSession,
    section: ResourceSection,
) -> str:
    """Build ``"Parent \u203a Child \u203a Grandchild"`` by walking ``parent_id``.

    One ``session.get`` per ancestor — cheap at our typical depths (≤5).
    """
    titles: list[str] = []
    cur: ResourceSection | None = section
    while cur is not None:
        titles.append(cur.title)
        if cur.parent_id is None:
            break
        cur = await session.get(ResourceSection, cur.parent_id)
    titles.reverse()
    return " \u203a ".join(titles)
