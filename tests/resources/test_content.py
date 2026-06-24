"""``build_resource_block`` against the real ORM on async in-memory SQLite: one resource with three
top-level sections carving its ``raw_text`` into non-overlapping slices."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.db.models import Base, Resource, ResourceContent, ResourceSection
from rhizome.resources import build_index_block, build_resource_block, ResourceTreeNode as N

RAW = "0123456789AB"  # 12 chars: s10=[0:4)"0123", s11=[4:8)"4567", s12=[8:12)"89AB"


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Resource(id=1, name="Doc & <stuff>"))
        await s.flush()
        s.add(ResourceContent(resource_id=1, raw_text=RAW))
        s.add_all([
            ResourceSection(id=10, resource_id=1, title="Alpha", depth=0, position=0, start_offset=0),
            ResourceSection(id=11, resource_id=1, title="Beta", depth=0, position=1, start_offset=4),
            ResourceSection(id=12, resource_id=1, title="Gamma", depth=0, position=2, start_offset=8),
        ])
        await s.commit()
    yield factory
    await engine.dispose()


async def test_section_node_emits_only_its_slice(session_factory):
    async with session_factory() as s:
        block = await build_resource_block(s, 1, [N("section", 11)])
    assert '<resource id="1" name="Doc &amp; &lt;stuff&gt;">' in block   # name is xml-escaped
    assert '<section id="11" title="Beta">\n4567\n</section>' in block
    assert "0123" not in block and "89AB" not in block


async def test_resource_node_emits_full_raw_text(session_factory):
    async with session_factory() as s:
        block = await build_resource_block(s, 1, [N("resource", 1)])
    assert RAW in block


async def test_sections_render_in_document_order(session_factory):
    async with session_factory() as s:
        block = await build_resource_block(s, 1, [N("section", 12), N("section", 10)])
    assert block.index("Alpha") < block.index("Gamma")    # by start_offset, regardless of input order


async def test_missing_resource_yields_none(session_factory):
    async with session_factory() as s:
        assert await build_resource_block(s, 999, [N("resource", 999)]) is None


# ------------------------------------------------------------------------------------------------
# build_index_block — a single metadata-only listing, grouped by resource
# ------------------------------------------------------------------------------------------------

async def test_index_block_whole_resource_is_name_only(session_factory):
    async with session_factory() as s:
        block = await build_index_block(s, {1: [N("resource", 1)]})
    assert "<system>" in block and block.endswith("</system>")
    assert '<resource id="1" name="Doc &amp; &lt;stuff&gt;"/>' in block   # self-closing, name escaped
    assert "<section" not in block                                        # no per-section detail


async def test_index_block_partial_nests_section_titles(session_factory):
    async with session_factory() as s:
        block = await build_index_block(s, {1: [N("section", 11), N("section", 10)]})
    assert '<resource id="1" name="Doc &amp; &lt;stuff&gt;">' in block    # opening tag (has children)
    assert '<section id="10" title="Alpha"/>' in block
    assert '<section id="11" title="Beta"/>' in block
    assert block.index('id="10"') < block.index('id="11"')               # sorted by id


async def test_index_block_skips_stale_ids(session_factory):
    async with session_factory() as s:
        # A resource that no longer exists is dropped; with nothing left to list, the block is None.
        assert await build_index_block(s, {999: [N("resource", 999)]}) is None
        assert await build_index_block(s, {}) is None
