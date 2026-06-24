"""The resource vector index: DB-backed wholesale ingestion (``ResourceVectorStore.sync``), query
attribution, and the ``ResourceIndexStore.consume`` wiring that drives it.

Embeddings are seeded as orthonormal basis vectors (a 1.0 at a distinct dimension per chunk), so a query
equal to one basis vector returns exactly that chunk at score ~1.0 — retrieval ranking is deterministic
without a real embedding model.
"""

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.db.models import (
    Base,
    Resource,
    ResourceChunk,
    ResourceChunkSection,
    ResourceContent,
    ResourceSection,
)
from rhizome.resources import ResourceIndexStore, ResourceTree, ResourceTreeNode as N
from rhizome.resources.index import EXPECTED_DIM, ResourceVectorStore


def _emb(dim: int) -> bytes:
    """A unit basis vector at ``dim`` (1024-d voyage shape), packed as the stored float32 bytes."""
    vec = np.zeros(EXPECTED_DIM, dtype=np.float32)
    vec[dim] = 1.0
    return vec.tobytes()


def _emb_vec(dim: int) -> np.ndarray:
    return np.frombuffer(_emb(dim), dtype=np.float32)


@pytest.fixture
async def vector_db():
    """Resource 1 "Doc" with sections Intro(11) ⊃ Detail(101) and Body(12), and chunks:

        c0  -> section 11   (Intro),      embedding e0
        c1  -> section 12   (Body),       embedding e1
        c2  -> section 101  (Detail),     embedding e2
        c3  -> resource 1   (no section), embedding bytes of the WRONG length  -> skipped at build
        c4  -> resource 1   (no section), embedding NULL                       -> filtered by embedded_only
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(Resource(id=1, name="Doc"))
        await s.flush()
        s.add(ResourceContent(resource_id=1, raw_text="x" * 64))
        s.add_all([
            ResourceSection(id=11, resource_id=1, title="Intro", depth=0, position=0, start_offset=0),
            ResourceSection(id=101, resource_id=1, parent_id=11, title="Detail", depth=1, position=1, start_offset=4),
            ResourceSection(id=12, resource_id=1, title="Body", depth=0, position=2, start_offset=32),
        ])
        s.add_all([
            ResourceChunk(id=10, resource_id=1, chunk_index=0, start_offset=0, end_offset=4,
                          embedding=_emb(0), context_tag={"t": 0}),
            ResourceChunk(id=11, resource_id=1, chunk_index=1, start_offset=32, end_offset=36, embedding=_emb(1)),
            ResourceChunk(id=12, resource_id=1, chunk_index=2, start_offset=4, end_offset=8, embedding=_emb(2)),
            ResourceChunk(id=13, resource_id=1, chunk_index=3, start_offset=8, end_offset=12, embedding=b"\x00\x00\x00\x00"),
            ResourceChunk(id=14, resource_id=1, chunk_index=4, start_offset=12, end_offset=16, embedding=None),
        ])
        await s.flush()
        s.add_all([
            ResourceChunkSection(chunk_id=10, section_id=11),
            ResourceChunkSection(chunk_id=11, section_id=12),
            ResourceChunkSection(chunk_id=12, section_id=101),
        ])
        await s.commit()

    yield factory
    await engine.dispose()


# ------------------------------------------------------------------------------------------------
# Ingestion
# ------------------------------------------------------------------------------------------------

async def test_sync_resource_indexes_embedded_chunks_only(vector_db):
    store = ResourceVectorStore(vector_db)
    await store.sync([N("resource", 1)])

    # c0, c1, c2 index; the NULL-embedding chunk is filtered, the wrong-length one skipped at build.
    assert store.size == 3 and not store.is_empty()

    meta, score = (await store.query(_emb_vec(0), k=1))[0]
    assert meta.chunk_id == 10 and meta.resource_name == "Doc"
    assert meta.section_breadcrumb == ""        # pulled via the whole-resource node, no owning section
    assert meta.context_tag == {"t": 0} and score == pytest.approx(1.0, abs=1e-5)


async def test_sync_section_carries_breadcrumb_and_scopes_chunks(vector_db):
    store = ResourceVectorStore(vector_db)
    await store.sync([N("section", 101)])

    assert store.size == 1
    meta, _ = (await store.query(_emb_vec(2), k=3))[0]
    assert meta.chunk_id == 12 and meta.section_breadcrumb == "Intro › Detail"


async def test_sync_is_wholesale_rebuild(vector_db):
    store = ResourceVectorStore(vector_db)
    await store.sync([N("section", 11)])
    assert store.size == 1 and (await store.query(_emb_vec(0), k=1))[0][0].chunk_id == 10

    # Re-syncing a different scope replaces the index entirely — the prior chunk is gone.
    await store.sync([N("section", 12)])
    assert store.size == 1
    meta, _ = (await store.query(_emb_vec(1), k=1))[0]
    assert meta.chunk_id == 11 and meta.section_breadcrumb == "Body"


async def test_sync_empty_clears(vector_db):
    store = ResourceVectorStore(vector_db)
    await store.sync([N("resource", 1)])
    await store.sync([])
    assert store.is_empty() and store.size == 0
    assert await store.query(_emb_vec(0), k=1) == []


async def test_query_guards(vector_db):
    store = ResourceVectorStore(vector_db)
    assert await store.query(_emb_vec(0), k=1) == []   # empty index
    await store.sync([N("resource", 1)])
    assert await store.query(_emb_vec(0), k=0) == []   # non-positive k


# ------------------------------------------------------------------------------------------------
# consume() wiring
# ------------------------------------------------------------------------------------------------

async def test_consume_populates_and_clears_attached_index(vector_db):
    tree = ResourceTree(vector_db)
    await tree.refresh()
    index = ResourceVectorStore(vector_db)
    store = ResourceIndexStore(tree, index=index)

    store.set_loaded(N("section", 11), True)
    await store.consume()
    assert index.size == 1 and store.consumed == store.loaded == {N("section", 11)}

    # Steady state: no delta -> no rebuild work (index untouched).
    await store.consume()
    assert index.size == 1

    # Unloading everything drives a clear on the next consume.
    store.set_loaded(N("section", 11), False)
    await store.consume()
    assert index.is_empty() and store.consumed == frozenset()
