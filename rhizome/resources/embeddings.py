"""Embedding helpers — chunking, embedding, and storage for resource documents."""

from __future__ import annotations

import asyncio
import struct
from typing import Protocol

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rhizome.credentials import APIKeyService
from rhizome.db.operations import add_chunks, get_resource, link_chunks_to_sections
from rhizome.logs import get_logger

_log = get_logger("resources.embeddings")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=100,
    add_start_index=True,
)


def chunk_text(text: str) -> list[dict]:
    """Split text into chunks with positional metadata.

    Returns list of dicts with chunk_index, start_offset, end_offset.
    """
    docs = _splitter.create_documents([text])
    return [
        {
            "chunk_index": idx,
            "start_offset": doc.metadata["start_index"],
            "end_offset": doc.metadata["start_index"] + len(doc.page_content),
        }
        for idx, doc in enumerate(docs)
    ]


# ---------------------------------------------------------------------------
# Embedding service (VoyageAI via REST)
#
# We call the Voyage REST endpoint directly instead of the `voyageai` Python SDK
# because the SDK (v0.3.7) crashes on import with our Pydantic 2.x setup.
# The issue is in voyageai/object/multimodal_embeddings.py: a Pydantic v1
# compat-layer model uses Field(..., min_items=1), which raises ValueError
# in the v1 shim. The langchain-voyageai wrapper depends on this SDK so it's
# also unusable. Replace with the SDK once upstream ships a fix.
# (Encountered 2026-03-27)
# ---------------------------------------------------------------------------


# ==========================================================================================
# Service: EmbeddingService
#   Shape : protocol + first-party impl (VoyageEmbedder, below)
#   Scope : root  ·  optional -- registered only when a provider is configured (resolve with try_get)
# ==========================================================================================


class EmbeddingService(Protocol):
    """Text → vectors: the injectable embedding capability, one implementation per provider.

    ``embed`` takes any number of texts and batches internally; ``dimension`` is the length of each
    returned vector (the vector store validates chunk embeddings against it). Registered only when a
    provider is configured, so consumers resolve it with ``ServiceAccessor.try_get`` and degrade when
    it is ``None`` — embedding is an optional feature.
    """

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VoyageEmbedder(EmbeddingService):
    """``EmbeddingService`` backed by the Voyage REST API.

    Holds an injected ``APIKeyService`` and resolves the ``voyage`` key per call, so a rotated key is
    picked up without reconstruction. ``embed`` batches in groups of 128 and retries on 429 with
    capped exponential backoff.
    """

    _MODEL_DIMS = {"voyage-3.5": 1024}
    _BATCH = 128

    def __init__(self, api_keys: APIKeyService, *, model: str = "voyage-3.5") -> None:
        if model not in self._MODEL_DIMS:
            raise ValueError(f"Unknown Voyage model {model!r}; known: {sorted(self._MODEL_DIMS)}")
        self._api_keys = api_keys
        self._model = model

    @property
    def dimension(self) -> int:
        return self._MODEL_DIMS[self._model]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        api_key = self._api_keys.require("voyage")
        out: list[list[float]] = []
        for i in range(0, len(texts), self._BATCH):
            out.extend(await self._embed_batch(texts[i:i + self._BATCH], api_key))
        return out

    async def _embed_batch(self, texts: list[str], api_key: str) -> list[list[float]]:
        """One REST call for a single ≤128 batch, retrying on 429 with capped backoff."""
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(5):
                resp = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"input": texts, "model": self._model},
                )
                if resp.status_code == 429:
                    await asyncio.sleep(min(2 ** attempt, 16))
                    continue
                resp.raise_for_status()
                data = resp.json()["data"]
                data.sort(key=lambda x: x["index"])
                return [d["embedding"] for d in data]
            resp.raise_for_status()
            return []  # unreachable, but satisfies the type checker


def floats_to_bytes(floats: list[float]) -> bytes:
    """Pack a list of floats into raw bytes (float32)."""
    return struct.pack(f"{len(floats)}f", *floats)


async def embed_chunks(raw_text: str, chunks: list[dict], embedder: EmbeddingService) -> list[dict]:
    """Attach embedding bytes to each chunk dict, embedding the chunk slices via *embedder*."""
    texts = [raw_text[c["start_offset"]:c["end_offset"]] for c in chunks]
    embeddings = await embedder.embed(texts)
    for chunk, emb in zip(chunks, embeddings):
        chunk["embedding"] = floats_to_bytes(emb)
    return chunks


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

async def has_embeddings(session_factory, resource_id: int) -> bool:
    """Check if a resource has any chunks with embeddings."""
    async with session_factory() as session:
        resource = await get_resource(session, resource_id)
        if resource is None or not resource.chunks:
            return False
        return any(c.embedding is not None for c in resource.chunks)


async def compute_embeddings(session_factory, resource_id: int, embedder: EmbeddingService) -> None:
    """Chunk and embed a resource via *embedder*, storing results in the DB.

    Handles two cases:
        - No chunks exist yet: creates chunks from raw_text and embeds them.
        - Chunks exist but lack embeddings: computes and attaches embeddings.

    Raises on failure (API errors, missing raw_text, etc.).
    """
    async with session_factory() as session:
        resource = await get_resource(session, resource_id)
        if resource is None:
            raise ValueError(f"Resource {resource_id} not found.")
        if not resource.content or not resource.content.raw_text:
            raise ValueError(f"Resource {resource_id} has no raw_text to embed.")

        raw_text = resource.content.raw_text
        existing_chunks = resource.chunks

    if existing_chunks and all(c.embedding is None for c in existing_chunks):
        # Chunks exist but need embeddings — rebuild chunk dicts from DB rows
        # and compute embeddings for them.
        chunk_dicts = [
            {
                "chunk_index": c.chunk_index,
                "start_offset": c.start_offset,
                "end_offset": c.end_offset,
            }
            for c in existing_chunks
        ]
        chunk_dicts = await embed_chunks(raw_text, chunk_dicts, embedder)

        # Update existing chunk rows with embeddings.
        async with session_factory() as session:
            resource = await get_resource(session, resource_id)
            for chunk_obj, chunk_dict in zip(resource.chunks, chunk_dicts):
                chunk_obj.embedding = chunk_dict["embedding"]
            await session.commit()

        _log.info("Embedded %d existing chunks for resource %d", len(chunk_dicts), resource_id)
    else:
        # No chunks at all — create from scratch.
        chunk_dicts = chunk_text(raw_text)
        chunk_dicts = await embed_chunks(raw_text, chunk_dicts, embedder)

        async with session_factory() as session:
            await add_chunks(session, resource_id, chunk_dicts)
            await link_chunks_to_sections(session, resource_id)
            await session.commit()

        _log.info("Created and embedded %d chunks for resource %d", len(chunk_dicts), resource_id)
