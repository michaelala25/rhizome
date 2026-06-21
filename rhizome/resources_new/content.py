"""Turning a resource's loaded nodes into the text the model reads.

Free functions over the DB session — the prompt engine's resource passes call them and wrap the result
in well-known-id messages. They live here, between the stores and the engine, on purpose: the stores are
synchronous content-free containers, and the engine owns message identity and placement, not resource
text. Two shapes:

- ``build_resource_block`` (context channels) — one resource's loaded subtree as context-stuffed text.
- ``build_index_block`` (index channel) — a single metadata-only listing of everything searchable,
  grouped by resource. No content; the agent pulls passages through the search tool on demand.

Emission rule for ``build_resource_block`` (one block per node, no per-DB-section splitting):

- a ``("resource", rid)`` node -> the resource's full ``raw_text``.
- a ``("section", sid)`` node  -> ``raw_text[start_offset : effective_end)``, the effective end being the
  start of the next section at the same or shallower depth (``compute_section_end_offsets``).

Callers pass only non-overlapping nodes — the stores' canonical minimal form guarantees this — so no
deduplication happens here.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from rhizome.db.operations import (
    compute_section_end_offsets,
    fetch_resource_labels,
    get_resource_with_content_and_sections,
)

from .tree import ResourceTreeNode


async def build_resource_block(
    session: AsyncSession,
    resource_id: int,
    nodes: list[ResourceTreeNode],
) -> str | None:
    """The context-stuffing block for one resource, or ``None`` when there is nothing to emit (resource
    or ``raw_text`` missing, or no node yields usable text). One DB read per call — cache at the call site
    (see ``ResourceContextStore.block``)."""
    resource = await get_resource_with_content_and_sections(session, resource_id)
    if resource is None:
        return None

    raw_text = resource.content.raw_text if resource.content is not None else None
    if not raw_text:
        return None

    sections_by_id = {s.id: s for s in resource.sections}
    section_ends = compute_section_end_offsets(resource.sections, len(raw_text))

    blocks: list[str] = []
    for node in sorted(nodes, key=lambda n: _node_sort_key(n, sections_by_id)):
        if node.kind == "resource":
            blocks.append(raw_text)
        else:
            section = sections_by_id.get(node.id)
            if section is None or section.start_offset is None:
                continue
            end = section_ends.get(section.id, len(raw_text))
            blocks.append(_format_section(section.id, section.title, raw_text[section.start_offset:end]))

    if not blocks:
        return None

    inner = "\n".join(blocks)
    return (
        "<system> The following resource content has been context-stuffed by the user:\n"
        f'<resource id="{resource.id}" name="{_xml_escape(resource.name)}">\n'
        f"{inner}\n"
        "</resource></system>"
    )


async def build_index_block(
    session: AsyncSession,
    grouped: dict[int, list[ResourceTreeNode]],
) -> str | None:
    """A single listing of everything queryable in the vector index, grouped by owning resource.

    ``grouped`` maps a resource id to its loaded nodes (the prompt engine groups the index store's flat
    ``loaded`` set this way). A whole-resource entry renders as just the name; a partially-indexed
    resource nests its loaded section titles. Metadata only — no content. ``None`` when nothing renders
    (e.g. every id is stale)."""
    if not grouped:
        return None

    section_ids = [n.id for nodes in grouped.values() for n in nodes if n.kind == "section"]
    names, titles = await fetch_resource_labels(session, list(grouped), section_ids)

    entries: list[str] = []
    for rid in sorted(grouped):
        name = names.get(rid)
        if name is None:
            continue   # resource deleted out from under the index
        if any(node.kind == "resource" for node in grouped[rid]):
            entries.append(f'<resource id="{rid}" name="{_xml_escape(name)}"/>')
            continue
        sections = [
            f'  <section id="{node.id}" title="{_xml_escape(titles[node.id])}"/>'
            for node in sorted(grouped[rid], key=lambda n: n.id) if node.id in titles
        ]
        if sections:
            entries.append(f'<resource id="{rid}" name="{_xml_escape(name)}">\n' + "\n".join(sections) + "\n</resource>")

    if not entries:
        return None

    listing = "\n".join(entries)
    return (
        "<system>The following resources have been indexed and can be searched for relevant passages "
        f"on demand:\n{listing}\n</system>"
    )


def _node_sort_key(node: ResourceTreeNode, sections_by_id: dict) -> tuple[int, int]:
    """Document order: the resource root first, then sections by ``start_offset``."""
    if node.kind == "resource":
        return (-1, 0)
    section = sections_by_id.get(node.id)
    start = getattr(section, "start_offset", None) if section is not None else None
    return (0, start if start is not None else 0)


def _format_section(section_id: int, title: str, text: str) -> str:
    return f'<section id="{section_id}" title="{_xml_escape(title)}">\n{text}\n</section>'


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
