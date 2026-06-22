"""Tests for the Mongo-style filter compiler, run against the real ORM models on in-memory SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from rhizome.agent.tools.utils import FilterError, compile_filter, compile_order_by
from rhizome.db.models import (
    Base,
    EntryType,
    Flashcard,
    FlashcardEntry,
    KnowledgeEntry,
    KnowledgeEntryTag,
    Tag,
    Topic,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed(s)
        yield s
    engine.dispose()


def _seed(s: Session) -> None:
    # Topic tree: Programming(1) > Git(2) > Git Internals(3); History(4) standalone.
    s.add_all([
        Topic(id=1, name="Programming"),
        Topic(id=2, name="Git", parent_id=1, description="Version control"),
        Topic(id=3, name="Git Internals", parent_id=2),
        Topic(id=4, name="History"),
    ])
    s.add_all([
        KnowledgeEntry(id=1, topic_id=2, title="Git Stash", content="git stash saves work",
                       entry_type=EntryType.fact),
        KnowledgeEntry(id=2, topic_id=3, title="Git Objects", content="blobs, trees, commits",
                       entry_type=EntryType.exposition),
        KnowledgeEntry(id=3, topic_id=4, title="Partition of India", content="1947 partition",
                       entry_type=EntryType.fact, difficulty=3),
        KnowledgeEntry(id=4, topic_id=1, title="Programming Overview", content="an overview"),
    ])
    s.add_all([Tag(id=1, name="vcs"), Tag(id=2, name="history")])
    s.add_all([
        KnowledgeEntryTag(knowledge_entry_id=1, tag_id=1),
        KnowledgeEntryTag(knowledge_entry_id=2, tag_id=1),
        KnowledgeEntryTag(knowledge_entry_id=3, tag_id=2),
    ])
    s.add_all([
        Flashcard(id=1, topic_id=2, question_text="What does git stash do?", answer_text="Saves work",
                  due=NOW - timedelta(days=1)),
        Flashcard(id=2, topic_id=4, question_text="When was the Partition?", answer_text="1947",
                  due=NOW + timedelta(days=1)),
        Flashcard(id=3, topic_id=2, question_text="What is a blob?", answer_text="File content object"),
    ])
    s.add_all([
        FlashcardEntry(flashcard_id=1, entry_id=1),
        FlashcardEntry(flashcard_id=2, entry_id=3),
        FlashcardEntry(flashcard_id=3, entry_id=1),
    ])
    s.flush()


def ids(session: Session, model: type, filter_: dict) -> set[int]:
    return set(session.scalars(select(model.id).where(compile_filter(model, filter_))))


# ------------------------------------------------------------------------------------------------------
# Scalar conditions and operators
# ------------------------------------------------------------------------------------------------------

def test_equality_shorthand(session):
    assert ids(session, Topic, {"name": "Git"}) == {2}


def test_null_shorthand_compiles_to_is_null(session):
    assert ids(session, KnowledgeEntry, {"entry_type": None}) == {4}


def test_sibling_fields_and_together(session):
    assert ids(session, KnowledgeEntry, {"topic_id": 2, "entry_type": "fact"}) == {1}


def test_contains_is_case_insensitive(session):
    assert ids(session, KnowledgeEntry, {"title": {"$contains": "git"}}) == {1, 2}


def test_like_uses_explicit_wildcards(session):
    assert ids(session, KnowledgeEntry, {"title": {"$like": "%Objects"}}) == {2}


def test_comparison_ops_and_sibling_ops_and_together(session):
    assert ids(session, KnowledgeEntry, {"difficulty": {"$gte": 1, "$lte": 5}}) == {3}
    assert ids(session, Topic, {"id": {"$gt": 2}}) == {3, 4}


def test_in_and_nin(session):
    assert ids(session, Topic, {"id": {"$in": [1, 4]}}) == {1, 4}
    assert ids(session, Topic, {"id": {"$nin": [1, 4]}}) == {2, 3}


def test_exists_on_column(session):
    assert ids(session, Flashcard, {"due": {"$exists": False}}) == {3}
    assert ids(session, Topic, {"description": {"$exists": True}}) == {2}


# ------------------------------------------------------------------------------------------------------
# Boolean structure
# ------------------------------------------------------------------------------------------------------

def test_or_tree(session):
    found = ids(session, KnowledgeEntry, {"$or": [
        {"title": {"$contains": "stash"}},
        {"title": {"$contains": "partition"}},
    ]})
    assert found == {1, 3}


def test_not_follows_sql_null_semantics(session):
    # SQL three-valued logic: NOT (NULL = 'fact') is unknown, so entry 4 (entry_type NULL) is excluded.
    assert ids(session, KnowledgeEntry, {"$not": {"entry_type": "fact"}}) == {2}
    # Matching NULLs too requires saying so, as in SQL:
    found = ids(session, KnowledgeEntry, {"$or": [{"$not": {"entry_type": "fact"}}, {"entry_type": None}]})
    assert found == {2, 4}


def test_empty_filter_matches_everything(session):
    assert ids(session, Topic, {}) == {1, 2, 3, 4}


# ------------------------------------------------------------------------------------------------------
# Relationship traversal
# ------------------------------------------------------------------------------------------------------

def test_dotted_path_through_junction(session):
    assert ids(session, Flashcard, {"flashcard_entries.entry_id": {"$in": [1]}}) == {1, 3}
    assert ids(session, KnowledgeEntry, {"tags.name": "vcs"}) == {1, 2}


def test_dotted_path_through_to_one_relationship(session):
    assert ids(session, KnowledgeEntry, {"topic.name": {"$contains": "git"}}) == {1, 2}


def test_nested_subfilter_on_relationship(session):
    assert ids(session, KnowledgeEntry, {"tags": {"name": "history"}}) == {3}


def test_exists_on_relationship(session):
    assert ids(session, KnowledgeEntry, {"flashcard_entries": {"$exists": False}}) == {2, 4}


# ------------------------------------------------------------------------------------------------------
# $in_subtree
# ------------------------------------------------------------------------------------------------------

def test_in_subtree_includes_root_and_descendants(session):
    assert ids(session, KnowledgeEntry, {"topic_id": {"$in_subtree": 1}}) == {1, 2, 4}
    assert ids(session, KnowledgeEntry, {"topic_id": {"$in_subtree": 2}}) == {1, 2}


def test_in_subtree_accepts_multiple_roots(session):
    assert ids(session, KnowledgeEntry, {"topic_id": {"$in_subtree": [2, 4]}}) == {1, 2, 3}


# ------------------------------------------------------------------------------------------------------
# Coercion
# ------------------------------------------------------------------------------------------------------

def test_enum_coerced_by_value(session):
    assert ids(session, KnowledgeEntry, {"entry_type": "fact"}) == {1, 3}


def test_enum_rejects_unknown_value_listing_valid(session):
    with pytest.raises(FilterError, match="fact"):
        compile_filter(KnowledgeEntry, {"entry_type": "factoid"})


def test_datetime_coerced_from_iso_string(session):
    assert ids(session, Flashcard, {"due": {"$lte": NOW.isoformat()}}) == {1}
    # Naive ISO strings are assumed UTC for aware (UTCDateTime) columns.
    assert ids(session, Flashcard, {"due": {"$lte": "2026-06-12T12:00:00"}}) == {1}
    # 'Z' suffix accepted.
    assert ids(session, Flashcard, {"due": {"$gte": "2026-06-12T12:00:00Z"}}) == {2}


def test_numeric_string_coerced(session):
    assert ids(session, KnowledgeEntry, {"difficulty": "3"}) == {3}


# ------------------------------------------------------------------------------------------------------
# Ordering
# ------------------------------------------------------------------------------------------------------

def test_compile_order_by(session):
    stmt = select(Topic.id).order_by(*compile_order_by(Topic, ["-id"]))
    assert list(session.scalars(stmt)) == [4, 3, 2, 1]

    stmt = select(KnowledgeEntry.id).order_by(*compile_order_by(KnowledgeEntry, ["entry_type", "-id"]))
    assert list(session.scalars(stmt)) == [4, 2, 3, 1]  # NULLs first asc, then exposition, then facts desc by id


def test_order_by_rejects_relationships():
    with pytest.raises(FilterError, match="relationship"):
        compile_order_by(KnowledgeEntry, ["tags"])


# ------------------------------------------------------------------------------------------------------
# Validation errors
# ------------------------------------------------------------------------------------------------------

def test_unknown_field_lists_valid_fields():
    with pytest.raises(FilterError, match="topic_id"):
        compile_filter(KnowledgeEntry, {"topicid": 2})


def test_unknown_operator_lists_valid_operators():
    with pytest.raises(FilterError, match=r"\$contains"):
        compile_filter(KnowledgeEntry, {"title": {"$regex": "x"}})


def test_non_operator_key_in_column_condition():
    with pytest.raises(FilterError, match="operators"):
        compile_filter(KnowledgeEntry, {"title": {"contains": "x"}})


def test_dotted_path_through_column_rejected():
    with pytest.raises(FilterError, match="column"):
        compile_filter(KnowledgeEntry, {"title.length": 5})


def test_bare_scalar_on_relationship_rejected():
    with pytest.raises(FilterError, match="relationship"):
        compile_filter(KnowledgeEntry, {"topic": 2})


def test_in_requires_list():
    with pytest.raises(FilterError, match=r"\$in"):
        compile_filter(Topic, {"id": {"$in": 1}})


def test_and_requires_nonempty_list():
    with pytest.raises(FilterError, match="non-empty"):
        compile_filter(Topic, {"$and": []})


def test_unknown_boolean_operator():
    with pytest.raises(FilterError, match=r"\$nor"):
        compile_filter(Topic, {"$nor": [{"id": 1}]})
