"""CRUD operations for flashcards."""

from datetime import datetime, timezone
from typing import Iterable

from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from rhizome.db import (
    Flashcard,
    FlashcardEntry,
    ReviewSession,
)
from rhizome.logs import get_logger

_logger = get_logger("tools.flashcards")


async def create_flashcard(
    session: AsyncSession,
    *,
    topic_id: int,
    question_text: str,
    answer_text: str,
    entry_ids: list[int],
    testing_notes: str | None = None,
    session_id: int | None = None,
) -> Flashcard:
    """Create a flashcard with linked entries."""
    flashcard = Flashcard(
        topic_id=topic_id,
        question_text=question_text,
        answer_text=answer_text,
        testing_notes=testing_notes,
        session_id=session_id,
    )
    session.add(flashcard)
    await session.flush()

    for eid in entry_ids:
        session.add(FlashcardEntry(flashcard_id=flashcard.id, entry_id=eid))
    await session.flush()

    _logger.info(
        "Flashcard created: id=%d, topic=%d, entries=%d, session=%s",
        flashcard.id, topic_id, len(entry_ids), session_id,
    )
    return flashcard


async def list_flashcards_by_entries(
    session: AsyncSession,
    entry_ids: list[int],
) -> list[Flashcard]:
    """Return flashcards linked to any of the given entry IDs.

    Excludes flashcards belonging to ephemeral sessions.
    """
    result = await session.execute(
        select(Flashcard)
        .options(
            selectinload(Flashcard.flashcard_entries),
            selectinload(Flashcard.session),
        )
        .join(FlashcardEntry, Flashcard.id == FlashcardEntry.flashcard_id)
        .outerjoin(ReviewSession, Flashcard.session_id == ReviewSession.id)
        .where(
            FlashcardEntry.entry_id.in_(entry_ids),
            # Exclude flashcards from ephemeral sessions (allow session_id=NULL or non-ephemeral)
            (ReviewSession.id.is_(None)) | (ReviewSession.ephemeral == False),  # noqa: E712
        )
        .distinct()
    )
    return list(result.scalars().unique().all())


def _apply_linked_flashcard_filters(
    stmt, *, entry_ids: Iterable[int], search: str | None,
):
    """Shared filter for the windowed + count variants of ``list_flashcards_for_entries``.
    ``entry_ids`` is matched against ``FlashcardEntry.entry_id`` via ``IN (...)`` so a single
    flashcard linked to several of the provided entries shows up once (the SELECT-DISTINCT /
    COUNT-DISTINCT on the caller dedupes). ``search`` runs case-insensitive LIKE against question +
    answer text (no testing notes — they're authoring metadata, not user-facing content).
    Ephemeral-session flashcards are excluded the same way ``list_flashcards_by_entries`` excludes
    them. Callers must check for an empty iterable themselves (the empty-IN predicate would be a no-
    op in SQL, but our convention is "empty = no rows" — same as ``topic_ids`` / ``entry_types``)."""
    stmt = (
        stmt.join(FlashcardEntry, Flashcard.id == FlashcardEntry.flashcard_id)
        .outerjoin(ReviewSession, Flashcard.session_id == ReviewSession.id)
        .where(
            FlashcardEntry.entry_id.in_(entry_ids),
            (ReviewSession.id.is_(None)) | (ReviewSession.ephemeral == False),  # noqa: E712
        )
    )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Flashcard.question_text.ilike(pattern),
                Flashcard.answer_text.ilike(pattern),
            )
        )
    return stmt


async def list_flashcards_for_entries(
    session: AsyncSession,
    entry_ids: Iterable[int],
    *,
    search: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Flashcard]:
    """Windowed flashcards linked to **any** of the given entries (union), optionally narrowed by a
    question/answer search.

    Mirrors the shape of ``entries.list_entries_paginated`` so the browser tab's flashcard sub-VM
    can reuse the same page-then-count pattern. Stable order by ``id`` (no user-pickable sort axis
    yet — add one alongside the eventual flashcard-sort dialog).

    A flashcard that links to more than one of ``entry_ids`` shows up exactly once thanks to the
    ``DISTINCT`` on the SELECT. ``entry_ids=[]`` returns ``[]`` without issuing a query — same
    empty-iterable convention as the entries ops.
    """
    ids = list(entry_ids)
    if not ids:
        return []
    stmt = select(Flashcard).options(selectinload(Flashcard.session)).distinct()
    stmt = _apply_linked_flashcard_filters(stmt, entry_ids=ids, search=search)
    stmt = stmt.order_by(Flashcard.id.asc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_flashcards_for_entries(
    session: AsyncSession,
    entry_ids: Iterable[int],
    *,
    search: str | None = None,
) -> int:
    """Count companion to ``list_flashcards_for_entries``. Shares the filter helper so the count
    matches the window exactly. ``COUNT(DISTINCT Flashcard.id)`` so a flashcard linked to multiple
    of ``entry_ids`` is counted once. Empty iterable → 0 without a query."""
    ids = list(entry_ids)
    if not ids:
        return 0
    stmt = select(func.count(func.distinct(Flashcard.id))).select_from(Flashcard)
    stmt = _apply_linked_flashcard_filters(stmt, entry_ids=ids, search=search)
    result = await session.execute(stmt)
    return result.scalar_one()


async def link_flashcards_to_entry(
    session: AsyncSession,
    *,
    entry_id: int,
    flashcard_ids: Iterable[int],
) -> int:
    """Insert ``FlashcardEntry`` rows linking each flashcard to ``entry_id``. Idempotent —
    flashcards already linked to this entry are silently skipped to respect the
    ``UNIQUE(flashcard_id, entry_id)`` constraint. Caller commits.

    Returns the number of new link rows actually inserted (after deduping against existing
    ones), so the caller can log a useful "linked N" figure."""
    ids = list(flashcard_ids)
    if not ids:
        return 0
    existing = await session.execute(
        select(FlashcardEntry.flashcard_id).where(
            FlashcardEntry.entry_id == entry_id,
            FlashcardEntry.flashcard_id.in_(ids),
        )
    )
    existing_ids = set(existing.scalars().all())
    new_ids = [fid for fid in ids if fid not in existing_ids]
    for fid in new_ids:
        session.add(FlashcardEntry(flashcard_id=fid, entry_id=entry_id))
    await session.flush()
    return len(new_ids)


async def unlink_flashcards_from_entry(
    session: AsyncSession,
    *,
    entry_id: int,
    flashcard_ids: Iterable[int],
) -> int:
    """Delete ``FlashcardEntry`` rows for the given flashcards on ``entry_id``. Idempotent —
    ids that aren't currently linked are silently no-op'd. Caller commits.

    Returns the number of rows actually deleted."""
    ids = list(flashcard_ids)
    if not ids:
        return 0
    result = await session.execute(
        delete(FlashcardEntry).where(
            FlashcardEntry.entry_id == entry_id,
            FlashcardEntry.flashcard_id.in_(ids),
        )
    )
    await session.flush()
    return result.rowcount or 0


async def get_flashcards_by_ids(
    session: AsyncSession,
    flashcard_ids: list[int],
) -> list[Flashcard]:
    """Return flashcards by their IDs, with flashcard_entries eagerly loaded."""
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Flashcard)
        .options(selectinload(Flashcard.flashcard_entries))
        .where(Flashcard.id.in_(flashcard_ids))
    )
    return list(result.scalars().all())


def _apply_flashcard_pool_filters(
    stmt,
    *,
    topic_ids: Iterable[int] | None,
    exclude_ids: Iterable[int] | None,
    search: str | None,
):
    """Apply the shared (topic_ids, exclude_ids, search) filter to a SELECT on Flashcard. Used by
    the windowed + count variants of the relink-mode pool query.

    Semantics:
      * ``topic_ids=None`` — no topic filter (every topic). Empty iterable = "explicitly no
        topics selected" → no rows match (same convention as the entries filter).
      * ``exclude_ids`` — flashcards whose id is in this set are filtered out. ``None`` or empty
        means no exclusion. Used by the relink pool to drop the already-linked rows so they
        appear in the pinned section only, not duplicated in the remaining pool.
      * ``search`` — case-insensitive LIKE against question + answer text. No testing notes (same
        rationale as ``_apply_linked_flashcard_filters``).
      * Ephemeral-session flashcards are always excluded — they're transient authoring artifacts,
        not user-facing rows."""
    if topic_ids is not None:
        ids = list(topic_ids)
        if not ids:
            # Empty filter set: caller explicitly asked for "no topics", so no rows match.
            stmt = stmt.where(Flashcard.id.is_(None))
        else:
            stmt = stmt.where(Flashcard.topic_id.in_(ids))
    if exclude_ids is not None:
        excl = list(exclude_ids)
        if excl:
            stmt = stmt.where(~Flashcard.id.in_(excl))
    # Ephemeral exclusion via outerjoin on the linked-session row.
    stmt = stmt.outerjoin(ReviewSession, Flashcard.session_id == ReviewSession.id).where(
        (ReviewSession.id.is_(None)) | (ReviewSession.ephemeral == False),  # noqa: E712
    )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Flashcard.question_text.ilike(pattern),
                Flashcard.answer_text.ilike(pattern),
            )
        )
    return stmt


async def list_flashcards_paginated(
    session: AsyncSession,
    *,
    topic_ids: Iterable[int] | None = None,
    exclude_ids: Iterable[int] | None = None,
    search: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Flashcard]:
    """Return a window of flashcards scoped to a topic range, optionally excluding a set of ids
    and narrowed by a question/answer search.

    The relink-mode "remaining pool" query: shows flashcards within the current topic filter that
    aren't already linked to the cursor entry (``exclude_ids`` carries the linked-ids set). Stable
    order by ``id`` ascending — no user-pickable sort axis yet."""
    stmt = select(Flashcard).options(selectinload(Flashcard.session))
    stmt = _apply_flashcard_pool_filters(
        stmt, topic_ids=topic_ids, exclude_ids=exclude_ids, search=search,
    )
    stmt = stmt.order_by(Flashcard.id.asc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_flashcards(
    session: AsyncSession,
    *,
    topic_ids: Iterable[int] | None = None,
    exclude_ids: Iterable[int] | None = None,
    search: str | None = None,
) -> int:
    """Count companion to ``list_flashcards_paginated``. Shares the filter helper so the count
    matches the window exactly."""
    stmt = select(func.count()).select_from(Flashcard)
    stmt = _apply_flashcard_pool_filters(
        stmt, topic_ids=topic_ids, exclude_ids=exclude_ids, search=search,
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def list_flashcards_by_topic(
    session: AsyncSession,
    topic_id: int,
) -> list[Flashcard]:
    """Return flashcards for a topic, with entries and session eagerly loaded.

    Includes ephemeral flashcards (caller can check ``flashcard.session.ephemeral``).
    """
    result = await session.execute(
        select(Flashcard)
        .options(
            selectinload(Flashcard.flashcard_entries),
            selectinload(Flashcard.session),
        )
        .where(Flashcard.topic_id == topic_id)
    )
    return list(result.scalars().all())


async def count_flashcards_by_topic(
    session: AsyncSession,
    topic_id: int,
) -> int:
    """Return the number of flashcards for a topic (including ephemeral)."""
    result = await session.execute(
        select(func.count())
        .select_from(Flashcard)
        .where(Flashcard.topic_id == topic_id)
    )
    return result.scalar_one()


async def count_flashcards_by_topics(
    session: AsyncSession,
    topic_ids: Iterable[int],
) -> int:
    """Return the number of flashcards whose ``topic_id`` is in ``topic_ids``.

    Multi-topic generalisation of ``count_flashcards_by_topic`` — used by the browser's topic-summary
    panel to report the subtree flashcard count for a cursor-highlighted topic. Returns 0 for an empty
    iterable.
    """
    ids = list(topic_ids)
    if not ids:
        return 0
    result = await session.execute(
        select(func.count())
        .select_from(Flashcard)
        .where(Flashcard.topic_id.in_(ids))
    )
    return result.scalar_one()


def to_fsrs_card(fc: Flashcard) -> Card:
    """Build an in-memory ``fsrs.Card`` from a Flashcard ORM row.

    Defaults ``due`` to "now" for parked cards (``due IS NULL``); the
    caller can decide whether that defaulting is appropriate.
    """
    return Card(
        card_id=fc.id,
        state=State(fc.fsrs_state),
        step=fc.fsrs_step,
        stability=fc.stability,
        difficulty=fc.difficulty,
        due=fc.due if fc.due is not None else datetime.now(timezone.utc),
        last_review=fc.last_review,
    )


def apply_fsrs_card(fc: Flashcard, card: Card) -> None:
    """Write an in-memory ``fsrs.Card``'s scheduling fields onto a Flashcard
    ORM row. Caller is responsible for flush/commit."""
    fc.fsrs_state = card.state.value
    fc.fsrs_step = card.step
    fc.stability = card.stability
    fc.difficulty = card.difficulty
    fc.due = card.due
    fc.last_review = card.last_review


async def commit_fsrs_card(
    session: AsyncSession,
    flashcard_id: int,
    card: Card,
) -> Flashcard:
    """Persist an in-memory ``fsrs.Card``'s scheduling fields to the DB.

    Idempotent — calling repeatedly with the same Card writes the same
    state. Used by callers that own the FSRS Card in memory (e.g. the
    flashcard review widget) and want to flush their final state to the
    DB at end of session.
    """
    fc = await session.get(Flashcard, flashcard_id)
    if fc is None:
        raise ValueError(f"Flashcard {flashcard_id} not found")
    apply_fsrs_card(fc, card)
    await session.flush()
    return fc


async def apply_rating(
    session: AsyncSession,
    flashcard_id: int,
    rating: Rating,
    *,
    review_datetime: datetime | None = None,
) -> Flashcard:
    """Advance a flashcard's FSRS state by applying a user rating.

    Activates the card if it was parked (``due IS NULL``) — rating a card
    implies the user wants it scheduled from now on.
    """
    fc = await session.get(Flashcard, flashcard_id)
    if fc is None:
        raise ValueError(f"Flashcard {flashcard_id} not found")

    review_dt = review_datetime or datetime.now(timezone.utc)
    card = to_fsrs_card(fc)
    updated_card, _log = Scheduler().review_card(card, rating, review_dt)
    apply_fsrs_card(fc, updated_card)
    await session.flush()

    _logger.info(
        "Flashcard rated: id=%d, rating=%s, new_state=%s, due=%s",
        fc.id, rating.name, State(fc.fsrs_state).name, fc.due.isoformat(),
    )
    return fc


async def activate_flashcard(
    session: AsyncSession,
    flashcard_id: int,
    *,
    due: datetime | None = None,
) -> Flashcard:
    """Schedule a parked flashcard. No-op if already scheduled."""
    fc = await session.get(Flashcard, flashcard_id)
    if fc is None:
        raise ValueError(f"Flashcard {flashcard_id} not found")
    if fc.due is None:
        fc.due = due or datetime.now(timezone.utc)
        await session.flush()
        _logger.info("Flashcard activated: id=%d, due=%s", fc.id, fc.due.isoformat())
    return fc


async def get_due_flashcards(
    session: AsyncSession,
    *,
    topic_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[Flashcard]:
    """Return scheduled flashcards whose ``due`` has arrived, oldest-due first.

    Parked flashcards (``due IS NULL``) are excluded.
    """
    cutoff = now or datetime.now(timezone.utc)
    stmt = (
        select(Flashcard)
        .options(selectinload(Flashcard.flashcard_entries))
        .where(Flashcard.due.is_not(None), Flashcard.due <= cutoff)
        .order_by(Flashcard.due.asc())
    )
    if topic_id is not None:
        stmt = stmt.where(Flashcard.topic_id == topic_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_flashcard_entry_ids(
    session: AsyncSession,
    flashcard_id: int,
) -> list[int]:
    """Return the entry IDs linked to a flashcard."""
    result = await session.execute(
        select(FlashcardEntry.entry_id)
        .where(FlashcardEntry.flashcard_id == flashcard_id)
    )
    return list(result.scalars().all())
