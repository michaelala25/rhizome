"""CRUD operations for Topic objects (tree structure via adjacency list)."""

from typing import Iterable

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rhizome.db import Topic
from rhizome.logs import get_logger

_logger = get_logger("tools.topics")


async def create_topic(
    session: AsyncSession,
    *,
    name: str,
    parent_id: int | None = None,
    description: str | None = None,
) -> Topic:
    """Create a new topic, optionally as a child of another topic."""
    topic = Topic(parent_id=parent_id, name=name, description=description)
    session.add(topic)
    await session.flush()
    _logger.info("Topic created: id=%d, name=%r, parent=%s", topic.id, topic.name, parent_id)
    return topic


async def get_topic(
    session: AsyncSession,
    topic_id: int,
) -> Topic | None:
    """Return a topic by id, or None if not found."""
    return await session.get(Topic, topic_id)


async def list_root_topics(session: AsyncSession) -> list[Topic]:
    """Return all root topics (those with no parent)."""
    result = await session.execute(
        select(Topic).where(Topic.parent_id.is_(None))
    )
    return list(result.scalars().all())


async def list_children(
    session: AsyncSession,
    parent_id: int,
) -> list[Topic]:
    """Return all direct children of a topic."""
    result = await session.execute(
        select(Topic).where(Topic.parent_id == parent_id)
    )
    return list(result.scalars().all())


async def find_parent_topic_ids(
    session: AsyncSession,
    candidate_ids: Iterable[int],
) -> set[int]:
    """Return the subset of ``candidate_ids`` that have at least one child topic.

    Used by the browser topic tree to populate "is this node expandable?"
    hints for a batch of just-loaded topics in a single query, instead of an
    N+1 ``list_children`` per node.
    """
    ids = list(candidate_ids)
    if not ids:
        return set()
    result = await session.execute(
        select(Topic.parent_id).where(Topic.parent_id.in_(ids)).distinct()
    )
    return {row[0] for row in result}


async def get_subtree(
    session: AsyncSession,
    root_topic_id: int,
    *,
    max_depth: int = 10,
) -> list[dict]:
    """Return all descendants of a topic using a recursive CTE.

    Returns a list of {"topic": Topic, "depth": int} dicts,
    ordered by depth then id. The root itself is not included.
    """
    rows = await session.execute(
        text("""
            WITH RECURSIVE subtree(topic_id, depth) AS (
                SELECT id, 1
                FROM topic
                WHERE parent_id = :root_id
                UNION ALL
                SELECT t.id, subtree.depth + 1
                FROM topic t
                JOIN subtree ON t.parent_id = subtree.topic_id
                WHERE subtree.depth < :max_depth
            )
            SELECT topic_id, depth FROM subtree ORDER BY depth, topic_id
        """),
        {"root_id": root_topic_id, "max_depth": max_depth},
    )
    results = []
    for row in rows:
        topic = await session.get(Topic, row.topic_id)
        results.append({"topic": topic, "depth": row.depth})
    return results


async def expand_subtrees(
    session: AsyncSession,
    root_ids: Iterable[int],
    *,
    max_depth: int = 10,
) -> set[int]:
    """Return the union of the subtrees rooted at each id in ``root_ids``.

    Includes the roots themselves. Returns a flat ``set[int]`` (just IDs — no
    ORM hydration), suitable for feeding into a downstream ``topic_id IN (...)``
    filter. Empty input returns an empty set.

    Used by the browser topic tree to translate a multi-selection into the
    actual set of topics that should pass the filter, on the assumption that
    selecting a topic means "this topic and everything under it."
    """
    ids = list(root_ids)
    if not ids:
        return set()

    # Expanding bind param: SQLAlchemy fans this out to the right number of
    # placeholders at execute time, which keeps the statement cacheable across
    # different selection sizes without us assembling SQL by string formatting.
    stmt = text(
        """
        WITH RECURSIVE subtree(topic_id, depth) AS (
            SELECT id, 0 FROM topic WHERE id IN :root_ids
            UNION ALL
            SELECT t.id, subtree.depth + 1
            FROM topic t
            JOIN subtree ON t.parent_id = subtree.topic_id
            WHERE subtree.depth < :max_depth
        )
        SELECT DISTINCT topic_id FROM subtree
        """
    ).bindparams(bindparam("root_ids", expanding=True))

    result = await session.execute(stmt, {"root_ids": ids, "max_depth": max_depth})
    return {row.topic_id for row in result}


async def update_topic(
    session: AsyncSession,
    topic_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Topic:
    """Update a topic's fields. Only provided (non-None) fields are changed."""
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise ValueError(f"Topic {topic_id} not found")
    if name is not None:
        topic.name = name
    if description is not None:
        topic.description = description
    await session.flush()
    return topic


async def delete_topic(
    session: AsyncSession,
    topic_id: int,
) -> None:
    """Delete a topic. Cascades to entries via ORM relationship.

    Raises an integrity error if the topic has child topics;
    callers should handle children first.
    """
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise ValueError(f"Topic {topic_id} not found")
    await session.delete(topic)
    await session.flush()
    _logger.info("Topic deleted: id=%d", topic_id)


async def delete_topic_subtree(
    session: AsyncSession,
    root_id: int,
) -> set[int]:
    """Delete a topic and its entire subtree in one DELETE. The FK ``ON DELETE CASCADE`` on
    every ``topic.id`` reference (children, entries, flashcards, ...) handles the recursive
    cleanup at the DB level — we only have to enumerate the topic ids ourselves so the caller
    can update in-memory state (selection sets, tree nodes) without a follow-up query.

    Returns the set of deleted topic ids."""
    subtree_ids = await expand_subtrees(session, [root_id])
    if not subtree_ids:
        return set()
    await session.execute(delete(Topic).where(Topic.id.in_(subtree_ids)))
    await session.flush()
    _logger.info("Topic subtree deleted: root=%d, count=%d", root_id, len(subtree_ids))
    return subtree_ids
