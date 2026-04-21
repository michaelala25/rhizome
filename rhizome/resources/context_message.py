"""Utilities for building context-stuffing ``HumanMessage`` blocks.

The agent session consumes MDL state from :class:`ResourceManager` at the
start of each stream; any resource with at least one CONTEXT_STUFFED entry
produces one :class:`HumanMessage` here that is injected into the graph
state via the ``add_messages`` reducer.  The message has a deterministic
id (``rhizome-resource-ctx-{resource_id}``) so subsequent CS changes can
replace it in place without duplicating content.

Emission rule (one block per CS entry, no per-DB-section splitting):

- ``("resource", rid)`` entry → the full ``raw_text`` of the resource.
- ``("section", sid)`` entry → ``raw_text[start_offset : effective_end)``,
  where the effective end is the start offset of the next section at the
  same or shallower depth (or end of raw_text if none).

By the MDL invariant, CS entries within a single resource never overlap,
so no deduplication is required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from rhizome.db.models import Resource
from rhizome.db.operations import compute_section_end_offsets
from rhizome.logs import get_logger

if TYPE_CHECKING:
    from rhizome.resources.manager import NodeKey

_log = get_logger("resources.context_message")


CONTEXT_MESSAGE_ID_PREFIX = "rhizome-resource-ctx-"


def resource_context_message_id(resource_id: int) -> str:
    """Return the deterministic message id for a resource's context block."""
    return f"{CONTEXT_MESSAGE_ID_PREFIX}{resource_id}"


def build_resource_context_message(
    resource: Resource,
    cs_entries: set[NodeKey],
) -> HumanMessage | None:
    """Build the context-stuffing ``HumanMessage`` for a single resource.

    ``resource`` must have ``content`` and ``sections`` eagerly loaded (use
    :func:`rhizome.db.operations.resources.get_resource_with_content_and_sections`).
    ``cs_entries`` is the subset of MDL entries under this resource that are
    at :attr:`LoadMode.CONTEXT_STUFFED`; callers are expected to filter.

    Returns ``None`` if there is nothing to emit (empty entry list, missing
    ``raw_text``, or every entry's range is unusable).

    **No deduplication is performed.**  This function blindly emits one text
    block per entry in ``cs_entries`` — if two entries cover overlapping
    ranges of ``raw_text`` (e.g. a resource root entry alongside a section
    entry under the same resource, or ancestor/descendant section entries),
    the output will contain the overlapping text twice.  It is the caller's
    responsibility to pass only non-overlapping entries.  The MDL invariant
    maintained by :class:`ResourceManager` guarantees this, but direct
    callers must enforce it themselves.
    """
    if not cs_entries:
        return None

    raw_text = resource.content.raw_text if resource.content is not None else None
    if not raw_text:
        _log.warning("Resource %d has no raw_text; skipping context message", resource.id)
        return None

    sections_by_id = {s.id: s for s in resource.sections}
    section_ends = compute_section_end_offsets(resource.sections, len(raw_text))

    ordered = sorted(cs_entries, key=lambda e: _entry_sort_key(e, sections_by_id))

    blocks: list[str] = []
    for entry in ordered:
        kind, nid = entry
        if kind == "resource":
            if nid != resource.id:
                _log.warning(
                    "Entry %r does not belong to resource %d; skipping", entry, resource.id
                )
                continue
            blocks.append(raw_text)
        else:
            section = sections_by_id.get(nid)
            if section is None:
                _log.warning(
                    "Section %d not found on resource %d; skipping", nid, resource.id
                )
                continue
            if section.start_offset is None:
                _log.warning(
                    "Section %d has no start_offset; skipping", nid
                )
                continue
            end = section_ends.get(section.id, len(raw_text))
            text = raw_text[section.start_offset : end]
            blocks.append(_format_section_block(section.id, section.title, text))

    if not blocks:
        return None

    inner = "\n".join(blocks)
    content = (
        f"[System] The following resource content has been context-stuffed "
        f"by the user:\n"
        f'<resource id="{resource.id}" name="{_xml_escape(resource.name)}">\n'
        f"{inner}\n"
        f"</resource>"
    )
    return HumanMessage(content=content, id=resource_context_message_id(resource.id))


def _entry_sort_key(entry: NodeKey, sections_by_id: dict[int, object]) -> tuple[int, int]:
    """Document-order sort key: resource root first, sections by start_offset."""
    kind, nid = entry
    if kind == "resource":
        return (-1, 0)
    section = sections_by_id.get(nid)
    start = getattr(section, "start_offset", None) if section is not None else None
    return (0, start if start is not None else 0)


def _format_section_block(section_id: int, title: str, text: str) -> str:
    return (
        f'<section id="{section_id}" title="{_xml_escape(title)}">\n'
        f"{text}\n"
        f"</section>"
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
