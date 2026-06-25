"""Tests for the query/insert/update/delete tools, run against the real ORM models on async in-memory
SQLite. Exercises the standalone ``run_*`` coroutines directly (the langchain ``@tool`` wrappers are thin
pass-throughs), with foreign-key enforcement ON so cascade behaviour is real."""

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.agent.tools.database import (
    run_aggregate,
    run_delete,
    run_insert,
    run_query,
    run_update,
)
from rhizome.agent.tools.tables import ALLOWED_TABLES, TableHooks, TableSpec
from rhizome.db.models import (
    Base,
    EntryType,
    Flashcard,
    KnowledgeEntry,
    RelatedKnowledgeEntries,
    Tag,
    Topic,
)

REG = ALLOWED_TABLES


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Programming(1) > Git(2) > Git Internals(3); History(4) standalone. Flush the self-referential
        # topic rows first: in one combined flush the unit of work doesn't reliably order an adjacency
        # list parent-before-child against dependent tables, tripping FK enforcement.
        s.add_all([
            Topic(id=1, name="Programming"),
            Topic(id=2, name="Git", parent_id=1, description="Version control"),
            Topic(id=3, name="Git Internals", parent_id=2),
            Topic(id=4, name="History"),
        ])
        await s.flush()
        s.add_all([
            KnowledgeEntry(id=1, topic_id=2, title="Git Stash", content="git stash saves work",
                           entry_type=EntryType.fact),
            KnowledgeEntry(id=2, topic_id=3, title="Git Objects", content="blobs, trees, commits",
                           entry_type=EntryType.exposition),
            KnowledgeEntry(id=3, topic_id=4, title="Partition of India", content="1947 partition",
                           entry_type=EntryType.fact, difficulty=3),
            Tag(id=1, name="vcs"),
            Flashcard(id=1, topic_id=2, question_text="What does git stash do?", answer_text="Saves work"),
        ])
        await s.commit()

    yield factory
    await engine.dispose()


async def _count(sessions, model, **eq) -> int:
    clause = [getattr(model, k) == v for k, v in eq.items()]
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*clause))


# ------------------------------------------------------------------------------------------------------
# query
# ------------------------------------------------------------------------------------------------------

async def test_query_scan_renders_compact_table(sessions):
    out = await run_query(sessions, REG, "topic")
    assert "topic: 4 row(s) matched; showing 4" in out
    assert "columns:" in out and "name" in out      # column header declared once
    assert '"Programming"' in out                    # value sits in a positional JSON-array row
    assert "name: Programming" not in out            # not the repeated-key vertical shape


async def test_query_scan_table_is_json_safe(sessions):
    # A value containing the would-be delimiter stays unambiguous as a JSON string in the table.
    await run_insert(sessions, REG, "topic", values={"name": "A, B and C", "parent_id": 1})
    out = await run_query(sessions, REG, "topic", filter={"name": {"$contains": "A, B"}})
    assert '"A, B and C"' in out


async def test_query_filter_and_relationship_traversal(sessions):
    out = await run_query(sessions, REG, "knowledge_entry", filter={"topic.name": {"$contains": "git"}})
    assert "Git Stash" in out and "Git Objects" in out
    assert "Partition of India" not in out


async def test_query_default_omits_content_and_truncates_long_text(sessions):
    long_note = "x" * 500
    await run_insert(sessions, REG, "knowledge_entry",
                     values={"topic_id": 1, "title": "Long", "content": "the full body",
                             "additional_notes": long_note, "entry_type": "fact"})
    default = await run_query(sessions, REG, "knowledge_entry", filter={"title": "Long"})
    # content is omitted from the default projection — flagged as available, not shown inline...
    assert "the full body" not in default
    assert "content" in default                  # named in the "omitted columns" note
    # ...and a long default-shown column is truncated with a terse char-count marker.
    assert long_note not in default
    assert "[500 chars]" in default


async def test_query_explicit_projection_returns_full_text(sessions):
    long_text = "y" * 500
    await run_insert(sessions, REG, "knowledge_entry",
                     values={"topic_id": 1, "title": "Full", "content": long_text, "entry_type": "fact"})
    projected = await run_query(sessions, REG, "knowledge_entry", filter={"title": "Full"},
                                columns=["content"])
    assert long_text in projected
    assert "additional_notes" not in projected


async def test_query_elides_null_but_keeps_falsy(sessions):
    # difficulty is unset (null) on entry 1; speed_testable defaults to False (a real value).
    out = await run_query(sessions, REG, "knowledge_entry", filter={"id": 1},
                          columns=["id", "difficulty", "speed_testable"])
    assert "speed_testable: False" in out   # falsy-but-real value is kept
    assert "difficulty" not in out          # null column is dropped entirely


async def test_query_limit_offset_flags_more_rows(sessions):
    out = await run_query(sessions, REG, "topic", order_by=["id"], limit=2)
    assert "showing 2" in out
    # Budget hint: this output's size plus the projected cost of pulling the whole match.
    assert "tokens" in out and "full match" in out and "raise limit" in out


async def test_query_unknown_table_lists_allowed(sessions):
    out = await run_query(sessions, REG, "secrets")
    assert "Unknown table 'secrets'" in out
    assert "topic" in out


async def test_query_unknown_column_projection_errors(sessions):
    out = await run_query(sessions, REG, "topic", columns=["nope"])
    assert "no exposed column" in out


# ------------------------------------------------------------------------------------------------------
# aggregate
# ------------------------------------------------------------------------------------------------------

async def test_aggregate_count_all(sessions):
    out = await run_aggregate(sessions, REG, "knowledge_entry")
    assert out == "knowledge_entry: count=3"


async def test_aggregate_count_with_filter(sessions):
    out = await run_aggregate(sessions, REG, "knowledge_entry", filter={"entry_type": "fact"})
    assert out == "knowledge_entry: count=2"


async def test_aggregate_group_by_counts_per_group(sessions):
    # Entries sit one each under topics 2, 3, 4 — the list_topics entry-count use case.
    out = await run_aggregate(sessions, REG, "knowledge_entry", group_by="topic_id")
    assert "3 group(s)" in out
    assert "columns: topic_id, count" in out        # group/metric labels declared once
    assert "[2, 1]" in out and "[3, 1]" in out       # one positional row per group


async def test_aggregate_multiple_metrics(sessions):
    out = await run_aggregate(sessions, REG, "knowledge_entry", metrics=["count", "max:difficulty"])
    assert "count=3" in out and "max:difficulty=3" in out


async def test_aggregate_rejects_unknown_metric(sessions):
    out = await run_aggregate(sessions, REG, "knowledge_entry", metrics=["median:difficulty"])
    assert "Unknown metric" in out


async def test_aggregate_rejects_group_by_unexposed_column(sessions):
    out = await run_aggregate(sessions, REG, "flashcard", group_by="fsrs_state")
    assert "no exposed column" in out


async def test_aggregate_allowed_on_query_only_table(sessions):
    out = await run_aggregate(sessions, REG, "flashcard")
    assert out == "flashcard: count=1"


# ------------------------------------------------------------------------------------------------------
# insert
# ------------------------------------------------------------------------------------------------------

async def test_insert_single_row_returns_pk(sessions):
    out = await run_insert(sessions, REG, "topic", values={"name": "Networking", "parent_id": 1})
    assert "Inserted 1 row(s) into topic: id=5" in out
    assert await _count(sessions, Topic, name="Networking") == 1


async def test_insert_many_and_enum_coercion(sessions):
    out = await run_insert(sessions, REG, "knowledge_entry", values=[
        {"topic_id": 1, "title": "A", "content": "a", "entry_type": "fact"},
        {"topic_id": 1, "title": "B", "content": "b", "entry_type": "overview"},
    ])
    assert "Inserted 2 row(s)" in out
    async with sessions() as s:
        row = await s.scalar(select(KnowledgeEntry).where(KnowledgeEntry.title == "A"))
        assert row.entry_type is EntryType.fact


async def test_insert_rejects_non_writable_column(sessions):
    out = await run_insert(sessions, REG, "topic", values={"name": "X", "id": 99})
    assert "not writable" in out
    assert await _count(sessions, Topic, id=99) == 0


async def test_insert_rejects_bad_enum_listing_valid(sessions):
    out = await run_insert(sessions, REG, "knowledge_entry",
                           values={"topic_id": 1, "title": "T", "content": "c", "entry_type": "factoid"})
    assert "Error:" in out and "fact" in out


async def test_insert_on_query_only_table_is_rejected(sessions):
    out = await run_insert(sessions, REG, "flashcard",
                           values={"topic_id": 1, "question_text": "q", "answer_text": "a"})
    assert "not allowed" in out
    assert await _count(sessions, Flashcard) == 1   # unchanged


# ------------------------------------------------------------------------------------------------------
# update
# ------------------------------------------------------------------------------------------------------

async def test_update_preview_does_not_write(sessions):
    out = await run_update(sessions, REG, "knowledge_entry",
                           filter={"entry_type": "fact"}, values={"difficulty": 5})
    assert "would set difficulty=5 on 2 row(s)" in out
    assert "confirm=true" in out
    # Nothing changed.
    assert await _count(sessions, KnowledgeEntry, difficulty=5) == 0


async def test_update_confirm_applies(sessions):
    out = await run_update(sessions, REG, "knowledge_entry",
                           filter={"entry_type": "fact"}, values={"difficulty": 5}, confirm=True)
    assert "Updated 2 row(s)" in out
    assert await _count(sessions, KnowledgeEntry, difficulty=5) == 2


async def test_update_requires_filter(sessions):
    out = await run_update(sessions, REG, "topic", filter={}, values={"name": "X"})
    assert "non-empty filter" in out


async def test_update_preview_surfaces_table_note(sessions):
    out = await run_update(sessions, REG, "topic", filter={"id": 2}, values={"name": "Git VCS"})
    assert "Note:" in out and "cascades" in out


async def test_update_rejects_non_writable_column(sessions):
    out = await run_update(sessions, REG, "topic", filter={"id": 2}, values={"created_at": "2020-01-01"})
    assert "not writable" in out


# ------------------------------------------------------------------------------------------------------
# delete
# ------------------------------------------------------------------------------------------------------

async def test_delete_preview_does_not_write(sessions):
    out = await run_delete(sessions, REG, "knowledge_entry", filter={"id": 1})
    assert "would remove 1 row(s)" in out and "confirm=true" in out
    assert await _count(sessions, KnowledgeEntry, id=1) == 1


async def test_delete_confirm_cascades_via_fk(sessions):
    # Deleting Git (topic 2) cascades to Git Internals (3) and the entries beneath both.
    out = await run_delete(sessions, REG, "topic", filter={"id": 2}, confirm=True)
    assert "Deleted 1 row(s) from topic" in out
    assert await _count(sessions, Topic, id=3) == 0          # subtree gone
    assert await _count(sessions, KnowledgeEntry, id=1) == 0  # entry under Git gone
    assert await _count(sessions, KnowledgeEntry, id=3) == 1  # History's entry untouched


async def test_delete_requires_filter(sessions):
    out = await run_delete(sessions, REG, "knowledge_entry", filter={})
    assert "non-empty filter" in out


async def test_delete_on_query_only_table_is_rejected(sessions):
    out = await run_delete(sessions, REG, "flashcard", filter={"id": 1}, confirm=True)
    assert "not allowed" in out
    assert await _count(sessions, Flashcard, id=1) == 1


# ------------------------------------------------------------------------------------------------------
# write hooks (validate / normalize)
# ------------------------------------------------------------------------------------------------------

async def test_insert_edge_then_reverse_is_rejected_as_cycle(sessions):
    # The validate hook reuses the same reachability predicate as add_relation.
    ok = await run_insert(sessions, REG, "related_knowledge_entries",
                          values={"source_entry_id": 1, "target_entry_id": 2, "relationship_type": "rel"})
    assert "Inserted 1 row(s)" in ok
    cyc = await run_insert(sessions, REG, "related_knowledge_entries",
                           values={"source_entry_id": 2, "target_entry_id": 1, "relationship_type": "rel"})
    assert "cycle" in cyc
    # The rejected edge was rolled back, not written.
    assert await _count(sessions, RelatedKnowledgeEntries, source_entry_id=2, target_entry_id=1) == 0


async def test_normalize_hook_runs_before_insert(sessions):
    class LowerName(TableHooks):
        def normalize(self, op, row):
            return {**row, "name": row["name"].lower()} if "name" in row else row

    spec = TableSpec(Tag, "tags", ops=frozenset({"query", "insert"}),
                     writable=frozenset({"name"}), hooks=LowerName())
    out = await run_insert(sessions, {"tag": spec}, "tag", values={"name": "Networking"})
    assert "Inserted 1 row(s)" in out
    async with sessions() as s:
        names = set(await s.scalars(select(Tag.name)))
    assert "networking" in names and "Networking" not in names
