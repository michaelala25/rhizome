"""CRUD operations for flashcards."""

from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from rhizome.db import (
    Flashcard,
    FlashcardEntry,
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
    from rhizome.db import ReviewSession

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
