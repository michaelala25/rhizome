"""Relation edge management with cycle detection and dependency chain queries."""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rhizome.db import KnowledgeEntry, RelatedKnowledgeEntries
from rhizome.logs import get_logger

_logger = get_logger("tools.relations")


class CycleError(Exception):
    """Raised when adding a relation would create a cycle."""


async def would_create_cycle(
    session: AsyncSession,
    source_entry_id: int,
    target_entry_id: int,
) -> bool:
    """Check whether adding source→target would create a cycle.

    Walks forward from target; if it can reach source, a cycle exists. The acyclicity invariant for the
    entry graph: both ``add_relation`` and the generic insert tool's guard call this single predicate so
    the rule has one definition.
    """
    result = await session.execute(
        text("""
            WITH RECURSIVE reachable(entry_id) AS (
                SELECT :target_entry_id
                UNION
                SELECT r.target_entry_id
                FROM related_knowledge_entries r
                JOIN reachable ON r.source_entry_id = reachable.entry_id
            )
            SELECT 1 FROM reachable WHERE entry_id = :source_entry_id LIMIT 1
        """),
        {"source_entry_id": source_entry_id, "target_entry_id": target_entry_id},
    )
    return result.scalar_one_or_none() is not None


async def add_relation(
    session: AsyncSession,
    *,
    source_entry_id: int,
    target_entry_id: int,
    relationship_type: str,
) -> RelatedKnowledgeEntries:
    """Add a directed relation between two entries.

    Raises CycleError if the new edge would create a cycle.
    """
    # TODO: TOCTOU race — the cycle check and insert are not atomic. Concurrent
    # calls could both pass the check and create a cycle. Mitigate with
    # with_for_update() or a DB-level constraint.
    if await would_create_cycle(session, source_entry_id, target_entry_id):
        _logger.warning("Cycle detected: %d → %d", source_entry_id, target_entry_id)
        raise CycleError(
            f"Adding {source_entry_id} -> {target_entry_id} would create a cycle"
        )
    relation = RelatedKnowledgeEntries(
        source_entry_id=source_entry_id,
        target_entry_id=target_entry_id,
        relationship_type=relationship_type,
    )
    session.add(relation)
    await session.flush()
    _logger.info("Relation added: %d → %d", source_entry_id, target_entry_id)
    return relation


async def remove_relation(
    session: AsyncSession,
    *,
    source_entry_id: int,
    target_entry_id: int,
) -> None:
    """Remove a relation between two entries. Raises if not found."""
    relation = await session.get(
        RelatedKnowledgeEntries, (source_entry_id, target_entry_id)
    )
    if relation is None:
        raise ValueError(
            f"Relation {source_entry_id} -> {target_entry_id} not found"
        )
    await session.delete(relation)
    await session.flush()


async def get_related_entries(
    session: AsyncSession,
    entry_id: int,
) -> list[RelatedKnowledgeEntries]:
    """Return all outgoing relation edges for an entry (one level deep)."""
    result = await session.execute(
        select(RelatedKnowledgeEntries).where(
            RelatedKnowledgeEntries.source_entry_id == entry_id
        )
    )
    return list(result.scalars().all())


async def get_dependency_chain(
    session: AsyncSession,
    entry_id: int,
) -> list[dict]:
    """Return the transitive dependency chain for an entry.

    Uses a recursive CTE limited to depth 10.  Only follows
    ``depends_on`` edges.  Returns a list of ``{"entry": KnowledgeEntry, "depth": int}``.
    """
    rows = await session.execute(
        text("""
            WITH RECURSIVE deps(entry_id, depth) AS (
                SELECT target_entry_id, 1
                FROM related_knowledge_entries
                WHERE source_entry_id = :entry_id
                  AND relationship_type = 'depends_on'
                UNION
                SELECT r.target_entry_id, deps.depth + 1
                FROM related_knowledge_entries r
                JOIN deps ON r.source_entry_id = deps.entry_id
                WHERE r.relationship_type = 'depends_on'
                  AND deps.depth < 10
            )
            SELECT entry_id, depth FROM deps ORDER BY depth
        """),
        {"entry_id": entry_id},
    )

    results = []
    for row in rows:
        entry = await session.get(KnowledgeEntry, row.entry_id)
        results.append({"entry": entry, "depth": row.depth})
    return results
