"""The vector index behind the resource index channel: a flat FAISS store over the embedded chunks of
the currently-indexed resources/sections.

This is the container ``ResourceIndexStore.consume`` populates. It owns DB access (its own
``session_factory``) so ingestion is a self-contained ``await store.sync(nodes)`` — the store fetches the
embedded chunks for the desired node set, hydrates per-chunk attribution (:class:`ChunkMeta`), and rebuilds
the index. Two halves, kept apart:

- *Ingestion* reads the precomputed ``ResourceChunk.embedding`` bytes already in the DB — it never calls an
  embedding model. Producing those bytes (chunking + the Voyage round-trip) happens earlier, at resource
  load time, and is not this layer's concern.
- *Query* (:meth:`ResourceVectorStore.query`) takes a vector already embedded by the same model used at
  ingest time and returns the top-``k`` ``(ChunkMeta, score)`` matches. Embedding the query string is the
  caller's job (the retrieval tool's).

Rebuild is wholesale: any load change re-fetches the full desired set and rebuilds the flat ``IndexFlatIP``
from scratch. At our scale (tens of thousands of chunks) that is sub-second, and it sidesteps the
reference-counting an incremental add/remove would need — a single chunk can straddle two loaded sibling
sections, so evicting one node must not drop a chunk another still claims. CPU-bound work (numpy
conversion, L2 normalization, the FAISS build and search) runs in ``asyncio.to_thread`` to keep the event
loop responsive.

Embedding dimension is hard-coded to :data:`EXPECTED_DIM` (voyage-3.5 = 1024); chunks whose stored bytes
don't match are skipped with a warning. A per-resource ``embedding_model`` column keyed off the model would
remove the hard-coding — deferred.
"""

import asyncio
from dataclasses import dataclass
from typing import Iterable

import faiss
import numpy as np
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rhizome.db.models import Resource, ResourceSection
from rhizome.db.operations import get_chunks, get_chunks_for_section
from rhizome.logs import get_logger

from .tree import ResourceTreeNode

_log = get_logger("resources.vector_store")


EXPECTED_DIM = 1024


@dataclass
class ChunkMeta:
    """Hydrated attribution for a single indexed chunk — what a retrieval hit carries back to the agent."""

    chunk_id: int
    resource_id: int
    resource_name: str

    # Breadcrumb "Ch 3 › Gradient Descent" from the section the chunk was pulled under; empty for a chunk
    # pulled via its whole-resource node (no owning section in the loaded scope).
    section_breadcrumb: str
    context_tag: dict | None


# ========================================================================================================================
# VECTOR STORE
# ========================================================================================================================


class ResourceVectorStore:
    """Flat FAISS index over the chunks of the currently-indexed resources/sections.

    Owns its ``session_factory``: ``sync`` is the one ingestion entry point, so the store is a
    self-contained collaborator the ``ResourceIndexStore`` holds and drives from ``consume``.
    """

    def __init__(self, session_factory: "async_sessionmaker[AsyncSession]") -> None:
        self._session_factory = session_factory
        self._index: faiss.Index | None = None
        self._metas: list[ChunkMeta] = []

    @property
    def size(self) -> int:
        """Number of chunks currently indexed."""
        return 0 if self._index is None else self._index.ntotal

    def is_empty(self) -> bool:
        return self.size == 0

    async def sync(self, nodes: Iterable[ResourceTreeNode]) -> None:
        """Rebuild the index from the desired node set (wholesale).

        ``nodes`` is the index store's canonical-minimal ``loaded`` description: a ``("resource", rid)``
        node pulls all of that resource's embedded chunks, a ``("section", sid)`` node pulls the chunks
        linked to that section. Chunks shared across nodes (a sibling-straddling chunk, a section under an
        already-whole resource) are de-duplicated by chunk id — first node to claim a chunk wins its
        breadcrumb, which is arbitrary but rarely material. An empty ``nodes`` clears the store.
        """
        nodes = list(nodes)
        if not nodes:
            self._index, self._metas = None, []
            _log.info("Vector store cleared (no indexed nodes)")
            return

        entries: dict[int, tuple[ChunkMeta, bytes]] = {}
        async with self._session_factory() as session:
            for node in nodes:
                for meta, embedding in await _node_chunk_metas(session, node):
                    entries.setdefault(meta.chunk_id, (meta, embedding))

        self._index, self._metas = await asyncio.to_thread(_build_index, list(entries.values()))
        _log.info("Vector store rebuilt: %d chunk(s) indexed across %d node(s)", self.size, len(nodes))

    async def query(self, query_vec: np.ndarray, k: int) -> list[tuple[ChunkMeta, float]]:
        """Top-``k`` ``(ChunkMeta, score)`` matches for a query vector already embedded by the ingest-time
        model. L2-normalizes defensively; returns ``[]`` on an empty index or non-positive ``k``."""
        if self._index is None or self._index.ntotal == 0 or k <= 0:
            return []
        return await asyncio.to_thread(_search_index, self._index, self._metas, query_vec, k)


# ========================================================================================================================
# INGESTION (DB → metas + embeddings)
# ========================================================================================================================


async def _node_chunk_metas(
    session: AsyncSession, node: ResourceTreeNode
) -> list[tuple[ChunkMeta, bytes]]:
    """Fetch a single node's embedded chunks and hydrate their metas.

    Resource nodes pull every embedded chunk of the resource; section nodes pull the chunks linked through
    ``resource_chunk_section`` and additionally carry the section's ancestor-chain breadcrumb. A node whose
    row has vanished yields nothing.
    """
    if node.kind == "resource":
        resource = await session.get(Resource, node.id)
        if resource is None:
            _log.warning("Resource %d not found while building vector store", node.id)
            return []
        chunks = await get_chunks(session, node.id, embedded_only=True)
        breadcrumb = ""
    else:  # "section"
        section = await session.get(ResourceSection, node.id)
        if section is None:
            _log.warning("Section %d not found while building vector store", node.id)
            return []
        resource = await session.get(Resource, section.resource_id)
        if resource is None:
            _log.warning("Resource %d not found for section %d", section.resource_id, node.id)
            return []
        chunks = await get_chunks_for_section(session, node.id, embedded_only=True)
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


async def _section_breadcrumb(session: AsyncSession, section: ResourceSection) -> str:
    """Build ``"Parent › Child › Grandchild"`` by walking ``parent_id`` — one ``session.get`` per ancestor,
    cheap at our typical depths (≤5)."""
    titles: list[str] = []
    current: ResourceSection | None = section
    while current is not None:
        titles.append(current.title)
        if current.parent_id is None:
            break
        current = await session.get(ResourceSection, current.parent_id)
    titles.reverse()
    return " › ".join(titles)


# ========================================================================================================================
# FAISS (CPU-bound — run under asyncio.to_thread)
# ========================================================================================================================


def _build_index(
    entries: list[tuple[ChunkMeta, bytes]],
) -> tuple[faiss.Index | None, list[ChunkMeta]]:
    """Build a normalized ``IndexFlatIP`` from ``(meta, embedding_bytes)`` pairs, skipping rows whose bytes
    don't match :data:`EXPECTED_DIM` or whose vector has zero norm."""
    expected_bytes = EXPECTED_DIM * 4
    kept: list[ChunkMeta] = []
    vectors: list[np.ndarray] = []
    for meta, raw in entries:
        if len(raw) != expected_bytes:
            _log.warning(
                "Chunk %d has embedding length %d bytes; expected %d. Skipping.",
                meta.chunk_id, len(raw), expected_bytes,
            )
            continue
        vec = np.frombuffer(raw, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm <= 0.0:
            _log.warning("Chunk %d embedding has zero norm; skipping", meta.chunk_id)
            continue
        vectors.append(vec / norm)
        kept.append(meta)

    if not vectors:
        return None, []
    index = faiss.IndexFlatIP(EXPECTED_DIM)
    index.add(np.stack(vectors).astype(np.float32))
    return index, kept


def _search_index(
    index: faiss.Index,
    metas: list[ChunkMeta],
    query_vec: np.ndarray,
    k: int,
) -> list[tuple[ChunkMeta, float]]:
    q = np.asarray(query_vec, dtype=np.float32).reshape(1, -1)
    norm = float(np.linalg.norm(q))
    if norm > 0.0:
        q = q / norm
    scores, ids = index.search(q, min(k, index.ntotal))
    return [
        (metas[idx], float(score))
        for idx, score in zip(ids[0].tolist(), scores[0].tolist())
        if 0 <= idx < len(metas)
    ]
