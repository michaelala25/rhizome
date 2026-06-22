"""Tests for the allowed-tables registry and the schema-reference renderer (no DB needed)."""

from rhizome.agent.tools.tables import (
    ALLOWED_TABLES,
    PendingWrite,
    TableHooks,
    TableSpec,
    render_schema_reference,
)
from rhizome.db.models import Flashcard, KnowledgeEntry, Topic


# ------------------------------------------------------------------------------------------------------
# TableSpec column/op policy
# ------------------------------------------------------------------------------------------------------

def test_hidden_columns_are_not_exposed():
    spec = ALLOWED_TABLES["flashcard"]
    names = spec.column_names()
    assert "question_text" in names
    for internal in ("fsrs_state", "fsrs_step", "stability", "difficulty", "last_review"):
        assert internal not in names


def test_default_writable_excludes_pk_and_timestamps():
    # A spec without an explicit `writable` falls back to exposed, non-PK, non-timestamp columns.
    spec = TableSpec(KnowledgeEntry, "x", ops=frozenset({"query", "update"}))
    writable = spec.writable_columns()
    assert "title" in writable and "content" in writable
    assert "id" not in writable
    assert "created_at" not in writable and "updated_at" not in writable


def test_allows_reflects_ops():
    assert ALLOWED_TABLES["topic"].allows("delete")
    assert not ALLOWED_TABLES["flashcard"].allows("update")


def test_table_name_defaults_to_tablename():
    assert ALLOWED_TABLES["knowledge_entry"].model is KnowledgeEntry
    assert ALLOWED_TABLES["knowledge_entry"].table_name == "knowledge_entry"


# ------------------------------------------------------------------------------------------------------
# Write-hook delegation (TableSpec -> TableHooks)
# ------------------------------------------------------------------------------------------------------

async def test_spec_without_hooks_is_noop():
    spec = ALLOWED_TABLES["topic"]
    assert spec.normalize("insert", {"name": "X"}) == {"name": "X"}
    await spec.validate(None, PendingWrite("insert", spec.model, rows=[{"name": "X"}]))  # must not raise


async def test_spec_delegates_to_hooks():
    seen = []

    class Recorder(TableHooks):
        def normalize(self, op, row):
            seen.append(("normalize", op))
            return {**row, "added": True}

        async def validate(self, session, write):
            seen.append(("validate", write.op))

    spec = TableSpec(Topic, "t", ops=frozenset({"insert"}), hooks=Recorder())
    assert spec.normalize("insert", {})["added"] is True
    await spec.validate(None, PendingWrite("insert", Topic))
    assert seen == [("normalize", "insert"), ("validate", "insert")]


# ------------------------------------------------------------------------------------------------------
# Schema reference rendering
# ------------------------------------------------------------------------------------------------------

def test_schema_reference_lists_allowed_tables_only():
    doc = render_schema_reference()
    for table in ALLOWED_TABLES:
        assert f"### {table} " in doc
    # A non-allowlisted table never appears as a section heading.
    assert "### resource_chunk " not in doc


def test_schema_reference_hides_hidden_columns_and_shows_ops():
    doc = render_schema_reference()
    assert "fsrs_state" not in doc          # hidden on flashcard
    assert "additional_args" not in doc     # hidden on review_session
    assert "Operations: query, insert, update, delete" in doc   # topic / knowledge_entry


def test_schema_reference_renders_enum_members_and_relationships():
    doc = render_schema_reference()
    assert "enum(fact|exposition|overview)" in doc
    assert "tags → tag (many)" in doc
    assert "topic → topic (one)" in doc


def test_schema_reference_surfaces_writable_and_notes():
    doc = render_schema_reference()
    assert "Writable: description, name, parent_id" in doc
    assert "cascades to its subtopics" in doc          # topic note
    assert "acyclic" in doc                             # related_knowledge_entries note
