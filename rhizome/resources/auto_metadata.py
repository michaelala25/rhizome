"""Automatic title and summary extraction from resource text.

Uses the first *token_budget* tokens of the document (counted
approximately) to generate a concise title and summary via a single
structured LLM call.

TODO (carry-over): no live consumer at the moment (used previously by the retired resource viewer).
Kept pending re-wiring into the new resource-creation flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field


# ── Structured output schema ──────────────────────────────────────


class ResourceMetadata(BaseModel):
    """LLM-generated title and summary for a resource."""

    title: str = Field(description="A concise, descriptive title for the document (3-10 words)")
    summary: str = Field(description="A 2-3 sentence summary of the document's content and purpose")
    ambiguous: bool = Field(
        description="True if the provided text is insufficient to determine a meaningful title or summary "
        "(e.g. garbled text, boilerplate, or otherwise uninterpretable content)",
        default=False,
    )


# ── Prompt ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are cataloguing resources for a knowledge management system. Each \
resource is a document (article, book chapter, webpage, paper, etc.) that \
a user has imported. Your job is to generate a title and summary from the \
opening text of the document.

The title is displayed in the UI and used by an AI agent to identify the \
resource. The summary is used by the agent to understand what the document \
contains without reading it in full.

Guidelines for the title:
- For scholarly articles, books, papers, or any document with a clear \
  title, extract the exact title as it appears in the text. Include the \
  first author last name with et al. for other authors, and the year of \
  publication in parentheses. For example, <name> - <author> (<year>).
- For webpages or informal documents without a clear title, write a \
  concise descriptive title (3-10 words).
- Do not invent a title that sounds more formal or specific than the \
  content warrants.

Guidelines for the summary:
- 2-3 sentences describing the document's subject matter, scope, and \
  intended audience or purpose.
- Be specific — mention key topics, not just the general domain.

Set "ambiguous" to true if the provided text is not useful enough to \
produce a meaningful title or summary (e.g. the text is garbled, consists \
only of boilerplate/legalese, is a bare table of contents, or is otherwise \
uninterpretable). When ambiguous, still provide your best-effort title and \
summary."""


# ── Public API ─────────────────────────────────────────────────────


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    """Return a prefix of *text* that fits within *token_budget* approximate tokens."""
    char_budget = token_budget * 4  # ~4 chars per token
    if len(text) <= char_budget:
        return text
    return text[:char_budget]


@dataclass
class MetadataResult:
    """Result of ``generate_resource_metadata``."""
    metadata: ResourceMetadata
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


async def generate_resource_metadata(
    llm: BaseChatModel,
    raw_text: str,
    *,
    token_budget: int = 1024,
) -> MetadataResult:
    """Generate a title and summary for a resource from its text.

    Parameters
    ----------
    llm:
        A LangChain chat model instance.
    raw_text:
        The full document text.
    token_budget:
        Maximum approximate tokens to send from the document (default 1024).
        Only a prefix of the text is used.

    Returns
    -------
    MetadataResult
        The generated metadata and token usage.
    """
    truncated = _truncate_to_token_budget(raw_text, token_budget)

    structured_llm = llm.with_structured_output(ResourceMetadata, include_raw=True)
    result = await structured_llm.ainvoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=truncated),
    ])
    metadata: ResourceMetadata = result["parsed"]
    usage = getattr(result["raw"], "usage_metadata", None) or {}
    return MetadataResult(
        metadata=metadata,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )
