"""Database operations for resources and resource chunks."""

from __future__ import annotations

from collections.abc import Iterable

from rhizome.logs import get_logger

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from rhizome.db.models import (
    LoadingPreference,
    Resource,
    ResourceChunk,
    ResourceChunkSection,
    ResourceContent,
    ResourceSection,
    TopicResource,
)


async def create_resource(
    session: AsyncSession,
    *,
    name: str,
    raw_text: str,
    content_hash: str | None = None,
    summary: str | None = None,
    estimated_tokens: int | None = None,
    loading_preference: LoadingPreference = LoadingPreference.auto,
    source_type: str | None = None,
    source_bytes: bytes | None = None,
) -> Resource:
    """Create a new resource with its content stored separately."""
    resource = Resource(
        name=name,
        content_hash=content_hash,
        summary=summary,
        estimated_tokens=estimated_tokens,
        loading_preference=loading_preference,
        source_type=source_type,
    )
    session.add(resource)
    await session.flush()

    content = ResourceContent(
        resource_id=resource.id,
        raw_text=raw_text,
        source_bytes=source_bytes,
    )
    session.add(content)
    await session.flush()
    resource.content = content
    return resource


async def get_resource(
    session: AsyncSession,
    resource_id: int,
) -> Resource | None:
    """Get a resource by ID, eagerly loading chunks and content."""
    result = await session.execute(
        select(Resource)
        .where(Resource.id == resource_id)
        .options(
            selectinload(Resource.chunks),
            selectinload(Resource.content),
        )
    )
    return result.scalar_one_or_none()


async def get_resource_with_content_and_sections(
    session: AsyncSession,
    resource_id: int,
) -> Resource | None:
    """Get a resource by ID, eagerly loading content and sections.

    Used by the context-stuffing pipeline to build ``HumanMessage`` blocks
    for the agent: the caller needs ``resource.content.raw_text`` plus the
    full section list to compute per-section text ranges.
    """
    result = await session.execute(
        select(Resource)
        .where(Resource.id == resource_id)
        .options(
            selectinload(Resource.content),
            selectinload(Resource.sections),
        )
    )
    return result.scalar_one_or_none()


async def list_resources(session: AsyncSession) -> list[Resource]:
    """List all resources (without chunks or raw_text body)."""
    result = await session.execute(
        select(Resource).order_by(Resource.created_at.desc())
    )
    return list(result.scalars().all())


def _apply_resource_pool_filters(stmt, *, exclude_ids: Iterable[int] | None, search: str | None):
    """Apply the shared (exclude_ids, search) filter to a SELECT on Resource. Shared by the windowed
    and count variants of the linker's pool query so the count matches the window exactly.

    ``exclude_ids`` drops resources already linked to the topic (they belong in the linker's pinned
    section, not the pool); ``None`` or empty means no exclusion. ``search`` is a case-insensitive
    LIKE against name + summary."""
    if exclude_ids is not None:
        excl = list(exclude_ids)
        if excl:
            stmt = stmt.where(~Resource.id.in_(excl))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Resource.name.ilike(pattern), Resource.summary.ilike(pattern)))
    return stmt


async def list_resources_paginated(
    session: AsyncSession,
    *,
    exclude_ids: Iterable[int] | None = None,
    search: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Resource]:
    """A window of resources, optionally excluding a set of ids and narrowed by a name/summary search.

    The linker's "remaining pool" query: every resource not already linked to the current topic
    (``exclude_ids`` carries the linked-ids set). Stable order by name."""
    stmt = _apply_resource_pool_filters(select(Resource), exclude_ids=exclude_ids, search=search)
    stmt = stmt.order_by(Resource.name).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_resources(
    session: AsyncSession,
    *,
    exclude_ids: Iterable[int] | None = None,
    search: str | None = None,
) -> int:
    """Count companion to ``list_resources_paginated``; shares the filter helper so the count matches
    the window exactly."""
    stmt = _apply_resource_pool_filters(
        select(func.count()).select_from(Resource), exclude_ids=exclude_ids, search=search,
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def delete_resource(
    session: AsyncSession,
    resource_id: int,
) -> None:
    """Delete a resource by ID. Raises ValueError if not found."""
    resource = await session.get(Resource, resource_id)
    if resource is None:
        raise ValueError(f"Resource {resource_id} not found.")
    await session.delete(resource)
    await session.flush()


async def update_resource(
    session: AsyncSession,
    resource_id: int,
    *,
    name: str | None = None,
    summary: str | None = None,
    estimated_tokens: int | None = None,
    loading_preference: LoadingPreference | None = None,
) -> Resource:
    """Partial update of a resource. Only modifies non-None fields."""
    resource = await session.get(Resource, resource_id)
    if resource is None:
        raise ValueError(f"Resource {resource_id} not found.")
    if name is not None:
        resource.name = name
    if summary is not None:
        resource.summary = summary
    if estimated_tokens is not None:
        resource.estimated_tokens = estimated_tokens
    if loading_preference is not None:
        resource.loading_preference = loading_preference
    await session.flush()
    return resource


# -----------------------------------------------------------------------
# Topic–Resource links
# -----------------------------------------------------------------------

async def link_resource_to_topic(
    session: AsyncSession,
    *,
    resource_id: int,
    topic_id: int,
) -> None:
    """Link a resource to a topic. Idempotent."""
    existing = await session.get(TopicResource, (topic_id, resource_id))
    if existing is not None:
        return
    session.add(TopicResource(topic_id=topic_id, resource_id=resource_id))
    await session.flush()


async def unlink_resource_from_topic(
    session: AsyncSession,
    *,
    resource_id: int,
    topic_id: int,
) -> None:
    """Unlink a resource from a topic. No-op if not linked."""
    existing = await session.get(TopicResource, (topic_id, resource_id))
    if existing is not None:
        await session.delete(existing)
        await session.flush()


async def list_resources_for_topic(
    session: AsyncSession,
    topic_id: int,
    *,
    load_chunks: bool = False,
) -> list[Resource]:
    """List resources directly attached to a topic."""
    stmt = (
        select(Resource)
        .join(TopicResource, TopicResource.resource_id == Resource.id)
        .where(TopicResource.topic_id == topic_id)
        .order_by(Resource.name)
    )
    if load_chunks:
        stmt = stmt.options(selectinload(Resource.chunks))
    stmt = stmt.options(
        selectinload(Resource.sections).selectinload(ResourceSection.chunks)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# -----------------------------------------------------------------------
# Chunks
# -----------------------------------------------------------------------

async def add_chunks(
    session: AsyncSession,
    resource_id: int,
    chunks: list[dict],
) -> list[ResourceChunk]:
    """Bulk-insert chunks for a resource.

    Each dict in `chunks` should have: chunk_index, start_offset, end_offset,
    and optionally context_tag, embedding.
    """
    resource = await session.get(Resource, resource_id)
    if resource is None:
        raise ValueError(f"Resource {resource_id} not found.")
    chunk_objs = [
        ResourceChunk(resource_id=resource_id, **c)
        for c in chunks
    ]
    session.add_all(chunk_objs)
    await session.flush()
    return chunk_objs


async def clear_chunks(
    session: AsyncSession,
    resource_id: int,
) -> int:
    """Delete all chunks for a resource. Returns count deleted."""
    result = await session.execute(
        select(ResourceChunk).where(ResourceChunk.resource_id == resource_id)
    )
    chunks = result.scalars().all()
    count = len(chunks)
    for c in chunks:
        await session.delete(c)
    await session.flush()
    return count


async def get_chunks(
    session: AsyncSession,
    resource_id: int,
    *,
    embedded_only: bool = False,
) -> list[ResourceChunk]:
    """Get all chunks for a resource, ordered by chunk_index.

    When ``embedded_only`` is True, skip rows where ``embedding IS NULL``.
    """
    stmt = select(ResourceChunk).where(ResourceChunk.resource_id == resource_id)
    if embedded_only:
        stmt = stmt.where(ResourceChunk.embedding.is_not(None))
    result = await session.execute(stmt.order_by(ResourceChunk.chunk_index))
    return list(result.scalars().all())


async def get_chunks_for_section(
    session: AsyncSession,
    section_id: int,
    *,
    embedded_only: bool = False,
) -> list[ResourceChunk]:
    """Get all chunks linked to a section via ``resource_chunk_section``.

    Ordered by ``chunk_index``.  Links are populated at ingestion by
    :func:`link_chunks_to_sections`, which is the single source of truth
    for section/chunk membership.
    """
    stmt = (
        select(ResourceChunk)
        .join(ResourceChunkSection, ResourceChunkSection.chunk_id == ResourceChunk.id)
        .where(ResourceChunkSection.section_id == section_id)
    )
    if embedded_only:
        stmt = stmt.where(ResourceChunk.embedding.is_not(None))
    result = await session.execute(stmt.order_by(ResourceChunk.chunk_index))
    return list(result.scalars().all())


# -----------------------------------------------------------------------
# Sections
# -----------------------------------------------------------------------

async def get_section_resource_ids(
    session: AsyncSession,
    section_ids: list[int] | set[int] | tuple[int, ...],
) -> dict[int, int]:
    """Batch-resolve ``{section_id: resource_id}`` for the given section ids.

    Sections that don't exist (e.g. deleted) are absent from the result —
    callers should handle missing keys as "unknown owner".  Returns an empty
    dict when given no ids.
    """
    if not section_ids:
        return {}
    result = await session.execute(
        select(ResourceSection.id, ResourceSection.resource_id)
        .where(ResourceSection.id.in_(list(section_ids)))
    )
    return {row.id: row.resource_id for row in result.all()}


def compute_section_end_offsets(
    sections: list[ResourceSection],
    raw_text_len: int,
) -> dict[int, int]:
    """Return ``{section_id: end_offset}`` for sections with a ``start_offset``.

    A section's effective end is the ``start_offset`` of the next section
    (in document order) at depth ≤ this section's depth — i.e. the next
    sibling or parent-sibling — or ``raw_text_len`` if no such section
    exists.  Sections without a ``start_offset`` are skipped and absent
    from the result.

    This is the single source of truth for "where does a section end?":
    the section tree stores only start offsets, and ends are derived on
    demand to avoid drift when sections are inserted or deleted.
    """
    offset_sections = sorted(
        [s for s in sections if s.start_offset is not None],
        key=lambda s: s.start_offset,  # type: ignore[arg-type, return-value]
    )
    ends: dict[int, int] = {}
    for i, sec in enumerate(offset_sections):
        end = raw_text_len
        for j in range(i + 1, len(offset_sections)):
            if offset_sections[j].depth <= sec.depth:
                end = offset_sections[j].start_offset  # type: ignore[assignment]
                break
        ends[sec.id] = end
    return ends

async def insert_sections(
    session: AsyncSession,
    resource_id: int,
    sections: list,
    *,
    parent_id: int | None = None,
    _position: list[int] | None = None,
) -> None:
    """Recursively insert a tree of sections for a resource.

    *sections* should be a list of objects with ``title``, ``depth``,
    ``page`` (optional), ``start_offset`` (optional), and ``children``
    attributes — matching ``rhizome.resources.extraction.Section``.

    Flushes after each row to obtain IDs for parent-child linking.
    Does not commit.
    """
    if _position is None:
        _position = [0]
    for section in sections:
        row = ResourceSection(
            resource_id=resource_id,
            parent_id=parent_id,
            title=section.title,
            depth=section.depth,
            position=_position[0],
            page_start=getattr(section, "page", None),
            start_offset=getattr(section, "start_offset", None),
        )
        _position[0] += 1
        session.add(row)
        await session.flush()
        if section.children:
            await insert_sections(
                session, resource_id, section.children,
                parent_id=row.id, _position=_position,
            )


# -----------------------------------------------------------------------
# Chunk–Section linking
# -----------------------------------------------------------------------

async def link_chunks_to_sections(
    session: AsyncSession,
    resource_id: int,
) -> int:
    """Link a resource's chunks to its sections based on offset overlap.

    A section's effective range is [start_offset, next_sibling_start_offset).
    The last section (by position) extends to infinity.  A chunk is linked to
    every section whose range it overlaps.

    Returns the number of join rows inserted.
    """
    _log = get_logger("db.operations.resources")

    # Clear any existing links for this resource's chunks.
    existing = await session.execute(
        select(ResourceChunk.id).where(ResourceChunk.resource_id == resource_id)
    )
    chunk_ids = [row[0] for row in existing.all()]
    if chunk_ids:
        await session.execute(
            delete(ResourceChunkSection).where(
                ResourceChunkSection.chunk_id.in_(chunk_ids)
            )
        )

    chunk_result = await session.execute(
        select(ResourceChunk)
        .where(ResourceChunk.resource_id == resource_id)
        .order_by(ResourceChunk.chunk_index)
    )
    chunks = list(chunk_result.scalars().all())

    section_result = await session.execute(
        select(ResourceSection)
        .where(ResourceSection.resource_id == resource_id)
        .order_by(ResourceSection.position)
    )
    sections = list(section_result.scalars().all())

    if not chunks or not sections:
        return 0

    raw_text_result = await session.execute(
        select(ResourceContent.raw_text).where(
            ResourceContent.resource_id == resource_id,
        )
    )
    raw_text = raw_text_result.scalar_one_or_none()
    if not raw_text:
        _log.debug("No raw_text for resource %d; cannot link chunks", resource_id)
        return 0
    raw_text_len = len(raw_text)

    section_ends = compute_section_end_offsets(sections, raw_text_len)
    if not section_ends:
        _log.debug("No sections with start_offset for resource %d", resource_id)
        return 0

    offset_sections = [s for s in sections if s.id in section_ends]

    _log.debug(
        "link_chunks_to_sections: resource=%d, %d chunks, %d sections with offsets",
        resource_id, len(chunks), len(offset_sections),
    )
    for sec in offset_sections:
        _log.debug(
            "  section pos=%d depth=%d id=%d title=%r range=[%d, %d)",
            sec.position, sec.depth, sec.id, sec.title,
            sec.start_offset, section_ends[sec.id],
        )

    count = 0
    per_section_chunks: dict[int, list[tuple[int, int, int]]] = {}
    for chunk in chunks:
        for sec in offset_sections:
            if chunk.start_offset < section_ends[sec.id] and chunk.end_offset > sec.start_offset:
                session.add(ResourceChunkSection(chunk_id=chunk.id, section_id=sec.id))
                per_section_chunks.setdefault(sec.id, []).append(
                    (chunk.chunk_index, chunk.start_offset, chunk.end_offset)
                )
                count += 1

    for sec in offset_sections:
        linked = per_section_chunks.get(sec.id, [])
        chunk_detail = ", ".join(f"#{idx}[{s}:{e}]" for idx, s, e in linked) or "(none)"
        _log.debug(
            "  -> section id=%d title=%r range=[%d, %d): %d chunks: %s",
            sec.id, sec.title, sec.start_offset, section_ends[sec.id],
            len(linked), chunk_detail,
        )

    if count:
        await session.flush()
    return count
