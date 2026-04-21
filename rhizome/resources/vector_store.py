"""FAISS-backed vector store for in-scope (LOADED) resource chunks.

The store is rebuilt from scratch whenever :meth:`ResourceManager.consume`
detects a change in the set of LOADED MDL entries.  At our expected scale
(tens of thousands of chunks, with low-hundreds-of-thousands as an extreme
upper bound), ``IndexFlatIP`` rebuild is sub-second and avoids the
bookkeeping that incremental add/remove would require.

CPU-bound work (numpy conversion, L2 normalization, FAISS index build and
search) runs in :func:`asyncio.to_thread` so the event loop stays responsive.

Embedding dimension is hard-coded to :data:`EXPECTED_DIM` (voyage-3.5 =
1024).  Chunks whose embedding byte length does not match are skipped with
a warning — a proper fix is to add an ``embedding_model`` column to
``ResourceChunk`` and key the expected dim off it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import faiss
import numpy as np

from rhizome.logs import get_logger

_log = get_logger("resources.vector_store")


EXPECTED_DIM = 1024


@dataclass
class ChunkMeta:
    """Hydrated metadata for a single indexed chunk."""
    chunk_id: int
    resource_id: int
    resource_name: str
    
    # Breadcrumb "Ch 3 › Gradient Descent" built from the deepest section
    # containing the chunk; empty string when the chunk is not linked to any
    # section.
    section_breadcrumb: str
    context_tag: dict | None


class ResourceVectorStore:
    """Flat FAISS index over the currently-LOADED resource chunks."""

    def __init__(self) -> None:
        self._index: faiss.Index | None = None
        self._metas: list[ChunkMeta] = []

    def is_empty(self) -> bool:
        return self._index is None or self._index.ntotal == 0

    @property
    def size(self) -> int:
        return 0 if self._index is None else self._index.ntotal

    async def rebuild(
        self,
        entries: list[tuple[ChunkMeta, bytes]],
    ) -> None:
        """Rebuild the index from ``(meta, embedding_bytes)`` pairs.

        The numpy conversion and FAISS build run in a worker thread.  Entries
        with a wrong-length embedding or zero-norm vector are skipped with a
        warning.  Passing an empty list clears the store.
        """
        if not entries:
            self._index = None
            self._metas = []
            _log.info("Vector store cleared (no entries)")
            return

        self._index, self._metas = await asyncio.to_thread(_build_index, entries)
        _log.info("Vector store rebuilt: %d chunks indexed", self.size)

    async def query(
        self, query_vec: np.ndarray, k: int,
    ) -> list[tuple[ChunkMeta, float]]:
        """Return top-``k`` ``(meta, score)`` pairs for the query vector.

        Caller is expected to pass a vector already embedded by the same
        model used at index time; this method L2-normalizes defensively.
        """
        if self._index is None or self._index.ntotal == 0 or k <= 0:
            return []

        return await asyncio.to_thread(_search_index, self._index, self._metas, query_vec, k)

    async def clear(self) -> None:
        """Drop the index and metadata."""
        self._index = None
        self._metas = []
        _log.info("Vector store cleared")


def _build_index(
    entries: list[tuple[ChunkMeta, bytes]],
) -> tuple[faiss.Index | None, list[ChunkMeta]]:
    kept: list[ChunkMeta] = []
    vectors: list[np.ndarray] = []
    expected_bytes = EXPECTED_DIM * 4
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
    matrix = np.stack(vectors).astype(np.float32)
    index = faiss.IndexFlatIP(EXPECTED_DIM)
    index.add(matrix)
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
    effective_k = min(k, index.ntotal)
    scores, ids = index.search(q, effective_k)
    out: list[tuple[ChunkMeta, float]] = []
    for idx, score in zip(ids[0].tolist(), scores[0].tolist()):
        if idx < 0 or idx >= len(metas):
            continue
        out.append((metas[idx], float(score)))
    return out
