"""Resource ingestion — text extraction, token estimation, and resource creation.

TODO (carry-over): no live consumer at the moment — the resource viewer that drove ingestion was
retired. Kept as a utility pending re-wiring into the new resource flow; note it does not yet compute
chunk embeddings (see ``embeddings.py``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pymupdf
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from rhizome.db.models import LoadingPreference
from rhizome.db.operations import create_resource, link_resource_to_topic


def extract_text_from_file(path: str) -> str:
    """Extract raw text from a local file (PDF or plain text)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        doc = pymupdf.open(path)
        return "\n\n".join(page.get_text() for page in doc)

    # Default: read as plain text
    return p.read_text(encoding="utf-8")


async def fetch_webpage_text(url: str) -> str:
    """Fetch a webpage and extract its text content."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        if "application/pdf" in content_type:
            doc = pymupdf.open(stream=resp.content, filetype="pdf")
            return "\n\n".join(page.get_text() for page in doc)

        # HTML or plain text — strip tags if HTML
        text = resp.text
        if "text/html" in content_type:
            text = _strip_html(text)
        return text


def _strip_html(html: str) -> str:
    """Naive HTML tag stripping. Strips script/style blocks then all tags."""
    import re
    # Remove script and style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    """Approximate token count using langchain's estimator."""
    return count_tokens_approximately([HumanMessage(content=text)])


async def ingest_resource(
    session_factory,
    *,
    name: str,
    raw_text: str,
    topic_ids: list[int] | None = None,
    loading_preference: LoadingPreference = LoadingPreference.auto,
    summary: str | None = None,
    source_type: str | None = None,
    source_bytes: bytes | None = None,
) -> tuple[int, int]:
    """Create a resource from raw text and optionally link to topics.

    Returns (resource_id, estimated_tokens).
    """
    content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
    estimated_tokens = estimate_tokens(raw_text)

    async with session_factory() as session:
        resource = await create_resource(
            session,
            name=name,
            raw_text=raw_text,
            content_hash=content_hash,
            summary=summary,
            estimated_tokens=estimated_tokens,
            loading_preference=loading_preference,
            source_type=source_type,
            source_bytes=source_bytes,
        )
        await session.flush()
        resource_id = resource.id

        if topic_ids:
            for tid in topic_ids:
                await link_resource_to_topic(session, resource_id=resource_id, topic_id=tid)

        await session.commit()

    return resource_id, estimated_tokens
