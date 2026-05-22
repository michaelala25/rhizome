"""CRUD + search operations for KnowledgeEntry objects."""

from typing import Iterable, Literal

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from rhizome.db import KnowledgeEntry, Topic
from rhizome.db.models import EntryType
from rhizome.logs import get_logger

_logger = get_logger("tools.entries")


# Columns the browser pane is allowed to sort by. Keep this tight rather than
# accepting arbitrary strings — it makes the SQL untrusted-input-proof and
# documents the supported axes in one place. "type" and "topic" are
# non-column sorts (CASE expression and joined column respectively); see
# ``_sort_expression`` for the dispatch.
EntrySortKey = Literal[
    "id", "title", "type", "topic", "created_at", "updated_at",
]
_SIMPLE_SORT_COLUMNS = {
    "id": KnowledgeEntry.id,
    "title": KnowledgeEntry.title,
    "created_at": KnowledgeEntry.created_at,
    "updated_at": KnowledgeEntry.updated_at,
}


def _sort_expression(sort_by: EntrySortKey):
    """Return ``(expr, requires_topic_join)`` for a sort key.

    ``expr`` is the SQL expression to ``ORDER BY``; ``requires_topic_join``
    is True for sort keys that need a JOIN onto the ``Topic`` table
    (currently just ``"topic"``). Simple column sorts fall through the
    lookup; ``"type"`` uses a ``CASE`` to lock the semantic order
    ``fact → exposition → overview`` rather than the natural string sort
    (which would put ``exposition`` first); ``"topic"`` joins ``Topic``
    and sorts on a lowered name for case-insensitive comparison.
    """
    if sort_by in _SIMPLE_SORT_COLUMNS:
        return _SIMPLE_SORT_COLUMNS[sort_by], False
    if sort_by == "type":
        return (
            case(
                (KnowledgeEntry.entry_type == EntryType.fact, 0),
                (KnowledgeEntry.entry_type == EntryType.exposition, 1),
                (KnowledgeEntry.entry_type == EntryType.overview, 2),
                else_=3,
            ),
            False,
        )
    if sort_by == "topic":
        return func.lower(Topic.name), True
    raise ValueError(f"Unsupported sort key: {sort_by!r}")


async def create_entry(
    session: AsyncSession,
    *,
    topic_id: int,
    title: str,
    content: str,
    entry_type: EntryType | None = None,
    additional_notes: str = "",
    difficulty: int | None = None,
    speed_testable: bool = False,
) -> KnowledgeEntry:
    """Create a new knowledge entry under a topic."""
    entry = KnowledgeEntry(
        topic_id=topic_id,
        title=title,
        content=content,
        entry_type=entry_type,
        additional_notes=additional_notes,
        difficulty=difficulty,
        speed_testable=speed_testable,
    )
    session.add(entry)
    await session.flush()
    _logger.info("Entry created: id=%d, title=%r", entry.id, entry.title)
    return entry


async def get_entry(
    session: AsyncSession,
    entry_id: int,
) -> KnowledgeEntry | None:
    """Return an entry by id, or None if not found."""
    return await session.get(KnowledgeEntry, entry_id)


async def count_entries(
    session: AsyncSession,
    topic_id: int,
) -> int:
    """Return the number of entries for a topic."""
    result = await session.execute(
        select(func.count()).select_from(KnowledgeEntry)
        .where(KnowledgeEntry.topic_id == topic_id)
    )
    return result.scalar_one()


async def list_entries(
    session: AsyncSession,
    topic_id: int,
) -> list[KnowledgeEntry]:
    """Return all entries for a topic, ordered by created_at."""
    result = await session.execute(
        select(KnowledgeEntry)
        .where(KnowledgeEntry.topic_id == topic_id)
        .order_by(KnowledgeEntry.created_at)
    )
    return list(result.scalars().all())


async def update_entry(
    session: AsyncSession,
    entry_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    entry_type: EntryType | None = None,
    topic_id: int | None = None,
    additional_notes: str | None = None,
    difficulty: int | None = None,
    speed_testable: bool | None = None,
) -> KnowledgeEntry:
    """Update an entry's fields. Only provided (non-None) fields are changed.

    Note: ``topic_id`` is non-nullable on the model, so ``None`` here unambiguously means "skip" rather
    than "clear the topic" — matching the semantics of every other field on this op.
    """
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise ValueError(f"KnowledgeEntry {entry_id} not found")
    if title is not None:
        entry.title = title
    if content is not None:
        entry.content = content
    if entry_type is not None:
        entry.entry_type = entry_type
    if topic_id is not None:
        entry.topic_id = topic_id
    if additional_notes is not None:
        entry.additional_notes = additional_notes
    if difficulty is not None:
        entry.difficulty = difficulty
    if speed_testable is not None:
        entry.speed_testable = speed_testable
    await session.flush()
    return entry


async def delete_entry(
    session: AsyncSession,
    entry_id: int,
) -> None:
    """Delete a knowledge entry."""
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise ValueError(f"KnowledgeEntry {entry_id} not found")
    await session.delete(entry)
    await session.flush()
    _logger.info("Entry deleted: id=%d", entry_id)


async def search_entries(
    session: AsyncSession,
    query: str,
    *,
    topic_id: int | None = None,
) -> list[KnowledgeEntry]:
    """Search entries by LIKE on title + content.

    Optionally scope to a specific topic.
    """
    pattern = f"%{query}%"
    stmt = select(KnowledgeEntry).where(
        (KnowledgeEntry.title.ilike(pattern)) | (KnowledgeEntry.content.ilike(pattern))
    )
    if topic_id is not None:
        stmt = stmt.where(KnowledgeEntry.topic_id == topic_id)
    result = await session.execute(stmt)
    entries = list(result.scalars().all())
    _logger.debug("Search: query=%r, results=%d", query, len(entries))
    return entries


def _apply_entry_filters(
    stmt,
    *,
    topic_ids: Iterable[int] | None,
    search: str | None,
    entry_types: Iterable[EntryType] | None,
):
    """Apply the shared (topic_ids, search, entry_types) filter to a SELECT
    on KnowledgeEntry. ``None`` means "no filter on this axis"; an empty
    iterable means "explicitly nothing matches" (consistent across
    topic_ids and entry_types)."""
    if topic_ids is not None:
        ids = list(topic_ids)
        if not ids:
            # Empty filter set: caller explicitly asked for "no topics", so no rows match.
            # Force-empty via a contradiction rather than skipping the predicate.
            stmt = stmt.where(KnowledgeEntry.id.is_(None))
        else:
            stmt = stmt.where(KnowledgeEntry.topic_id.in_(ids))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                KnowledgeEntry.title.ilike(pattern),
                KnowledgeEntry.content.ilike(pattern),
            )
        )
    if entry_types is not None:
        types = list(entry_types)
        if not types:
            # Same empty-iterable contradiction as topic_ids: explicitly
            # "no types selected" means no rows.
            stmt = stmt.where(KnowledgeEntry.id.is_(None))
        else:
            stmt = stmt.where(KnowledgeEntry.entry_type.in_(types))
    return stmt


async def list_entries_paginated(
    session: AsyncSession,
    *,
    topic_ids: Iterable[int] | None = None,
    search: str | None = None,
    entry_types: Iterable[EntryType] | None = None,
    sort_by: EntrySortKey = "created_at",
    sort_dir: Literal["asc", "desc"] = "asc",
    limit: int = 500,
    offset: int = 0,
) -> list[KnowledgeEntry]:
    """Return a window of entries matching the given filters.

    Semantics:
      - ``topic_ids=None`` means "no topic filter" (every topic). An empty iterable
        means "no topics selected → no rows", which is distinct from ``None``.
        Callers that want a subtree filter should expand their selection via
        ``topics.expand_subtrees`` first.
      - ``search`` runs case-insensitive LIKE against ``title`` and ``content``.
      - ``entry_types=None`` means "no type filter" (every type). An empty
        iterable means "explicitly nothing matches" (same convention as
        ``topic_ids``).
      - Results are ordered by ``sort_by`` then by ``id`` to keep ordering stable
        across pages when the primary key has ties (e.g. multiple rows with the
        same ``created_at`` at second granularity).
    """
    expr, needs_topic_join = _sort_expression(sort_by)
    direction = expr.asc() if sort_dir == "asc" else expr.desc()
    # Stable tiebreaker on id so pagination doesn't shuffle rows with equal sort keys.
    tiebreaker = KnowledgeEntry.id.asc() if sort_dir == "asc" else KnowledgeEntry.id.desc()

    # ``flashcard_entries`` is eagerly loaded so the browser pane can render the linked-flashcard ids
    # column without an N+1 round-trip per row. With the index on ``flashcard_entry.entry_id`` added
    # alongside this load, the secondary IN-query is a single seek per matching join row.
    stmt = select(KnowledgeEntry).options(
        selectinload(KnowledgeEntry.topic),
        selectinload(KnowledgeEntry.flashcard_entries),
    )
    if needs_topic_join:
        stmt = stmt.join(Topic, KnowledgeEntry.topic_id == Topic.id)
    stmt = _apply_entry_filters(
        stmt, topic_ids=topic_ids, search=search, entry_types=entry_types,
    )
    stmt = stmt.order_by(direction, tiebreaker).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_entries_filtered(
    session: AsyncSession,
    *,
    topic_ids: Iterable[int] | None = None,
    search: str | None = None,
    entry_types: Iterable[EntryType] | None = None,
) -> int:
    """Return the count of entries matching the same filter as ``list_entries_paginated``.

    Split from the windowed fetch so the browser pane can decide independently
    whether the count is worth paying for (it scans the whole filtered set).
    """
    stmt = select(func.count()).select_from(KnowledgeEntry)
    stmt = _apply_entry_filters(
        stmt, topic_ids=topic_ids, search=search, entry_types=entry_types,
    )
    result = await session.execute(stmt)
    return result.scalar_one()
