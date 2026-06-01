"""Resource tools — loading and managing document resources for RAG."""

from __future__ import annotations

import hashlib
from typing import Literal

import numpy as np
from langchain.tools import tool
import pymupdf
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from sqlalchemy import select

from rhizome.agent.tools.visibility import ToolVisibility, tool_visibility
from rhizome.db.models import LoadingPreference, ResourceChunk, ResourceContent
from rhizome.db.operations import (
    add_chunks,
    create_resource,
    get_resource,
    list_resources,
)
from rhizome.logs import get_logger
from rhizome.resources import ResourceManager
from rhizome.resources.embeddings import (
    chunk_text,
    embed_batch,
    embed_chunks,
    get_voyage_api_key,
)

_log = get_logger("agent.tools.resources")


# ---------------------------------------------------------------------------
# Text extraction (via LangChain document loaders)
# ---------------------------------------------------------------------------

def _extract_text(source: str, source_type: str) -> str:
    """Extract raw text from a source path or string."""
    if source_type == "text":
        return source

    if source_type == "pdf":
        doc = pymupdf.open(source)
        return "\n\n".join(page.get_text() for page in doc)

    raise ValueError(f"Unsupported source type: {source_type}")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Approximate token count using langchain's estimator."""
    return count_tokens_approximately([HumanMessage(content=text)])


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

def build_resource_tools(
    session_factory,
    resource_manager: ResourceManager | None = None,
) -> dict:
    """Build resource management tools with session_factory closed over.

    ``resource_manager``, when provided, exposes the ``query_resources``
    retrieval tool by closing over the manager's live vector store — there
    is no other clean way to give a stateless tool a handle to the index
    that the UI rebuilds on every loader toggle.
    """

    # ------------------------------------------------------------------
    # Intentionally disabled (2026-04-21):
    #   - add_resource is outdated — ingestion now happens through the UI's
    #     resource loader, and we don't want the agent creating rows with
    #     this stale flow.
    #   - list_resources / get_resource_info are pure DB reads that don't
    #     reflect loader state (INDEX / CONTEXT) or vector-store
    #     membership.  Revisit once we decide how much of that the agent
    #     should see.
    # ------------------------------------------------------------------

    # @tool_visibility(ToolVisibility.DEFAULT)
    # @tool("add_resource", description=(
    #     "Load a document as a resource for RAG. Extracts text, stores it in "
    #     "the database, and optionally creates vector embeddings for retrieval. "
    #     "Returns a resource manifest with ID, name, token estimate, and chunk count."
    # ))
    # async def add_resource_tool(
    #     name: str,
    #     source: str,
    #     source_type: Literal["text", "pdf"] = "text",
    #     loading_preference: Literal["auto", "context_stuff", "vector_store"] = "auto",
    #     blocking: bool = True,
    # ) -> str:
    #     # 1. Extract text
    #     try:
    #         raw_text = _extract_text(source, source_type)
    #     except Exception as e:
    #         return f"Error extracting text: {e}"
    #
    #     if not raw_text.strip():
    #         return "Error: no text content extracted from source."
    #
    #     # 2. Compute metadata
    #     content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    #     estimated_tokens = _estimate_tokens(raw_text)
    #     pref = LoadingPreference(loading_preference)
    #
    #     # 3. Create resource row
    #     async with session_factory() as session:
    #         resource = await create_resource(
    #             session,
    #             name=name,
    #             raw_text=raw_text,
    #             content_hash=content_hash,
    #             estimated_tokens=estimated_tokens,
    #             loading_preference=pref,
    #         )
    #         await session.commit()
    #         resource_id = resource.id
    #
    #     # 4. Chunk and embed
    #     chunks = chunk_text(raw_text)
    #     chunk_count = len(chunks)
    #
    #     should_embed = pref in (LoadingPreference.auto, LoadingPreference.vector_store)
    #
    #     if should_embed and blocking:
    #         try:
    #             api_key = get_voyage_api_key()
    #             chunks = await embed_chunks(raw_text, chunks, api_key)
    #         except Exception as e:
    #             # Store chunks without embeddings, report the error
    #             async with session_factory() as session:
    #                 await add_chunks(session, resource_id, chunks)
    #                 await session.commit()
    #             return (
    #                 f"Resource [{resource_id}] '{name}' created "
    #                 f"({estimated_tokens} tokens, {chunk_count} chunks). "
    #                 f"Embedding failed: {e}"
    #             )
    #
    #     # Store chunks (with or without embeddings)
    #     async with session_factory() as session:
    #         await add_chunks(session, resource_id, chunks)
    #         await session.commit()
    #
    #     # 5. Build manifest
    #     status = "indexed" if should_embed and blocking else "stored (no embeddings)"
    #     return (
    #         f"Resource [{resource_id}] '{name}' created.\n"
    #         f"  Tokens: ~{estimated_tokens}\n"
    #         f"  Chunks: {chunk_count}\n"
    #         f"  Loading preference: {loading_preference}\n"
    #         f"  Status: {status}"
    #     )

    # @tool_visibility(ToolVisibility.DEFAULT)
    # @tool("list_resources", description=(
    #     "List all loaded resources with their IDs, names, token estimates, "
    #     "loading preferences, and whether they have embeddings."
    # ))
    # async def list_resources_tool() -> str:
    #     async with session_factory() as session:
    #         resources = await list_resources(session)
    #
    #     if not resources:
    #         return "No resources loaded."
    #
    #     lines = []
    #     for r in resources:
    #         lines.append(
    #             f"- [{r.id}] {r.name} "
    #             f"(~{r.estimated_tokens or '?'} tokens, "
    #             f"pref={r.loading_preference.value})"
    #         )
    #     return f"{len(resources)} resource(s):\n" + "\n".join(lines)

    # @tool_visibility(ToolVisibility.DEFAULT)
    # @tool("get_resource_info", description=(
    #     "Get detailed info about a resource by ID: name, summary, token count, "
    #     "chunk count, and loading preference."
    # ))
    # async def get_resource_info_tool(resource_id: int) -> str:
    #     async with session_factory() as session:
    #         resource = await get_resource(session, resource_id)
    #
    #     if resource is None:
    #         return f"Resource {resource_id} not found."
    #
    #     has_embeddings = any(c.embedding is not None for c in resource.chunks)
    #     lines = [
    #         f"Resource [{resource.id}]: {resource.name}",
    #         f"  Tokens: ~{resource.estimated_tokens or '?'}",
    #         f"  Chunks: {len(resource.chunks)}",
    #         f"  Has embeddings: {has_embeddings}",
    #         f"  Loading preference: {resource.loading_preference.value}",
    #     ]
    #     if resource.summary:
    #         lines.append(f"  Summary: {resource.summary}")
    #     return "\n".join(lines)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("query_resources", description=(
        "Semantic search over the currently-indexed resource chunks. Embeds "
        "the query, runs it against the vector store built from the user's "
        "INDEX resources/sections, and returns the top-k matches with their "
        "resource name, section breadcrumb, similarity score, and chunk text. "
        "Returns an explanatory message if no resources are currently loaded."
    ))
    async def query_resources_tool(query: str, k: int = 5) -> str:
        if resource_manager is None:
            return (
                "query_resources is unavailable: this agent session was built "
                "without a ResourceManager."
            )
        if not query.strip():
            return "Error: query must not be empty."
        if k <= 0:
            return "Error: k must be a positive integer."

        store = resource_manager.vector_store
        if store.is_empty():
            return (
                "No resources are currently loaded into the vector store. "
                "Ask the user to toggle a resource or section to INDEX via "
                "the resource loader, then retry."
            )

        try:
            api_key = get_voyage_api_key()
            vecs = await embed_batch([query], api_key)
        except Exception as e:
            _log.exception("query_resources: failed to embed query")
            return f"Error embedding query: {e}"

        query_vec = np.asarray(vecs[0], dtype=np.float32)
        hits = await store.query(query_vec, k)
        if not hits:
            return f"No matches found for {query!r}."

        # Hydrate chunk text by joining chunk offsets against resource content
        chunk_ids = [meta.chunk_id for meta, _ in hits]
        async with session_factory() as session:
            chunk_rows = (await session.execute(
                select(ResourceChunk).where(ResourceChunk.id.in_(chunk_ids))
            )).scalars().all()
            chunks_by_id = {c.id: c for c in chunk_rows}

            rids = {c.resource_id for c in chunks_by_id.values()}
            raw_texts: dict[int, str] = {}
            if rids:
                content_rows = (await session.execute(
                    select(ResourceContent.resource_id, ResourceContent.raw_text)
                    .where(ResourceContent.resource_id.in_(rids))
                )).all()
                raw_texts = {r.resource_id: r.raw_text or "" for r in content_rows}

        lines = [f"{len(hits)} match(es) for {query!r}:"]
        for rank, (meta, score) in enumerate(hits, start=1):
            breadcrumb = meta.section_breadcrumb or "(no section)"
            chunk = chunks_by_id.get(meta.chunk_id)
            if chunk is None:
                text = "[chunk row not found]"
            else:
                raw = raw_texts.get(chunk.resource_id, "")
                text = raw[chunk.start_offset:chunk.end_offset] if raw else "[resource content missing]"
            lines.append(
                f"\n[{rank}] score={score:.3f} | {meta.resource_name} \u203a {breadcrumb} "
                f"(chunk_id={meta.chunk_id})"
            )
            lines.append(text)
        return "\n".join(lines)

    return {
        # "add_resource": add_resource_tool,
        # "list_resources": list_resources_tool,
        # "get_resource_info": get_resource_info_tool,
        "query_resources": query_resources_tool,
    }
