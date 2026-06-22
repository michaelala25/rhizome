"""Tests for the read-only SQL escape hatch, against a real on-disk SQLite file (``mode=ro`` needs one).

Each test drives the tool's coroutine with a stand-in runtime carrying the read-only factory on
``ctx.read_only_session_factory`` (the same factory the agent's context supplies), so the wiring under test
— the mode=ro engine and the authorizer — is exactly what the agent hits. The discriminating cases are
PRAGMA/ATTACH/CTE-hidden-write denial and blob redaction — those isolate the *authorizer's* contribution
from the mode=ro belt (which alone would block a plain INSERT anyway)."""

import os
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rhizome.agent.tools.sql import build_sql_tools, read_only_session_factory
from rhizome.db.models import Base, KnowledgeEntry, Resource, ResourceChunk, Topic


@pytest.fixture
async def sql_tool():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    rw = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with rw.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(rw, expire_on_commit=False)() as s:
        s.add_all([Topic(id=1, name="Programming"), Topic(id=2, name="Git", parent_id=1)])
        await s.flush()
        s.add_all([
            KnowledgeEntry(id=1, topic_id=2, title="Git Stash", content="git stash saves work"),
            Resource(id=1, name="Book"),
        ])
        await s.flush()
        # resource_chunk is NOT in the allowlist, and embedding is a LargeBinary blob → redaction target.
        s.add(ResourceChunk(id=1, resource_id=1, chunk_index=0, start_offset=0, end_offset=9,
                            embedding=b"\xde\xad\xbe\xef"))
        await s.commit()
    await rw.dispose()

    factory = read_only_session_factory(path)
    tool = build_sql_tools()["execute_sql"]
    runtime = SimpleNamespace(context=SimpleNamespace(read_only_session_factory=factory))

    async def run_sql(sql: str) -> str:
        return await tool.coroutine(sql=sql, runtime=runtime)

    yield run_sql

    await factory.kw["bind"].dispose()
    os.unlink(path)


# ------------------------------------------------------------------------------------------------------
# reads work — including the value-add (joins, full-schema reach)
# ------------------------------------------------------------------------------------------------------

async def test_select_returns_rows(sql_tool):
    run_sql = sql_tool
    out = await run_sql("SELECT name FROM topic ORDER BY id")
    assert "Programming" in out and "Git" in out


async def test_join_across_tables_works(sql_tool):
    run_sql = sql_tool
    out = await run_sql("SELECT t.name, e.title FROM knowledge_entry e JOIN topic t ON t.id = e.topic_id")
    assert "Git" in out and "Git Stash" in out


async def test_reaches_table_outside_allowlist(sql_tool):
    # resource_chunk is hidden from the structured tools — the escape hatch can still read it.
    run_sql = sql_tool
    out = await run_sql("SELECT id, chunk_index FROM resource_chunk")
    assert "chunk_index" in out


# ------------------------------------------------------------------------------------------------------
# authorizer: redaction + denial (the parts mode=ro alone does not give)
# ------------------------------------------------------------------------------------------------------

async def test_blob_column_redacted_to_null(sql_tool):
    run_sql = sql_tool
    out = await run_sql("SELECT id, embedding FROM resource_chunk")
    # The bytes were stored, but the authorizer substitutes NULL on read.
    assert "embedding" in out
    assert "null" in out and "dead" not in out.lower()


async def test_pragma_denied(sql_tool):
    # A read-only PRAGMA would succeed on a mode=ro connection — denial here proves the authorizer is live.
    run_sql = sql_tool
    out = await run_sql("PRAGMA table_info(topic)")
    assert "SQL error" in out and "not authorized" in out


async def test_attach_denied(sql_tool):
    run_sql = sql_tool
    out = await run_sql("ATTACH DATABASE ':memory:' AS evil")
    assert "SQL error" in out and "not authorized" in out


# ------------------------------------------------------------------------------------------------------
# writes blocked, no matter how they're disguised
# ------------------------------------------------------------------------------------------------------

async def test_insert_denied_and_nothing_written(sql_tool):
    run_sql = sql_tool
    out = await run_sql("INSERT INTO topic (name) VALUES ('Hacked')")
    assert "SQL error" in out and "read-only" in out
    # Confirm via a fresh read that nothing landed.
    check = await run_sql("SELECT count(*) FROM topic WHERE name = 'Hacked'")
    assert "\n0" in check


async def test_cte_hidden_write_denied(sql_tool):
    # Parser-level gating: a write that does not start with INSERT is still caught.
    run_sql = sql_tool
    out = await run_sql("WITH x AS (SELECT 1) INSERT INTO topic (name) SELECT 'sneaky'")
    assert "SQL error" in out
    check = await run_sql("SELECT count(*) FROM topic WHERE name = 'sneaky'")
    assert "\n0" in check
