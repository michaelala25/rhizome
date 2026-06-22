"""Token-usage accounting: what a thread's prompt costs, and where that cost goes.

Two fundamentally different numbers live here, and keeping them apart is the whole point of the module:

- **Provider ground truth** (``ProviderUsage``) — what the provider actually counted on a model call,
  read straight off an ``AIMessage``'s ``usage_metadata``. It is an aggregate (system + tools + messages
  all folded into one ``input_tokens``) and a *fact*: accurate, but not attributable to any one message.
  Cache reads / writes are subsets of ``input_tokens`` (per the ``UsageMetadata`` contract), so the cache
  split is part of this layer — this module is the one owner of reading it.
- **Local attribution** (``UsageSegment``) — an *estimate* of how the current prompt breaks down per
  message (plus a synthetic slice for the system prompt and one for the tool definitions). Approximate,
  but the only way to answer "what is eating my prefix budget". Estimated with langchain's
  ``count_tokens_approximately`` and then **normalized so the segments sum to the provider's
  ``input_tokens``** — which both makes the breakdown agree with the headline and folds the tool-schema
  cost (otherwise invisible) back into the picture.

``UsageReport`` is the two layers assembled for one thread, as of its latest model call. The prompt engine
produces it via ``PromptEngine.report`` (it holds the build-time system/tool/window constants the estimate
needs); everything here is provider-neutral and side-effect free.

A caveat the consumer should know: ``segments`` are estimated from the *current* checkpoint state while
``usage`` is from the *last* model call, so a compile that queued a not-yet-sent message can drift the two.
Normalization absorbs the drift as a rescale, so the breakdown reads as "current context, anchored to the
last measured size" — not "exactly what the last request billed".
"""

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately


# ========================================================================================================================
# DATA MODEL
# ========================================================================================================================


@dataclass(frozen=True)
class ProviderUsage:
    """One model call's token usage, exactly as the provider reported it (off an ``AIMessage``).

    ``input_tokens`` is the full prompt size — ``cache_read_tokens`` and ``cache_creation_tokens`` are
    *subsets* of it (the ``UsageMetadata`` contract: input is the sum of all input token types), never
    additions. The ground-truth layer: accurate and aggregate, not attributable per message.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int        # subset of input served from cache (cheap)
    cache_creation_tokens: int    # subset of input written to cache (premium)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def fresh_input_tokens(self) -> int:
        """Full-rate input: neither read from nor written to the cache."""
        return self.input_tokens - self.cache_read_tokens - self.cache_creation_tokens


@dataclass(frozen=True)
class UsageSegment:
    """One slice of the current prompt's estimated composition. ``message_id`` is ``None`` for the
    synthetic slices (the system block, the tool-definition block) and set for real messages — the
    per-message ids are what let a consumer attribute the prefix budget down to individual messages."""

    kind: str                       # "system" | "tools" | "user" | "agent" | "tool_result" | "resource" | ...
    tokens: int                     # normalized estimate (scaled so the segments sum to input_tokens)
    message_id: str | None = None
    label: str | None = None        # display hint: a resource id, a mode, ...


@dataclass(frozen=True)
class UsageReport:
    """One thread's picture as of its latest model call: the provider ground truth, the context-window
    ceiling, and a normalized per-segment breakdown of the current prompt."""

    usage: ProviderUsage | None         # None before the thread's first model response
    max_input_tokens: int | None        # context window; None if the model profile yields nothing
    segments: tuple[UsageSegment, ...]  # sums to usage.input_tokens once usage is known

    @property
    def usage_percent(self) -> float | None:
        """How full the context window is, ``0..100`` — ``None`` when either input usage or the window
        ceiling is unknown."""
        if self.usage is None or not self.max_input_tokens:
            return None
        return 100 * self.usage.input_tokens / self.max_input_tokens

    def by_kind(self) -> dict[str, int]:
        """Roll the per-message segments up into a category → tokens map (the shape a status display wants
        before drilling down to individual messages)."""
        out: dict[str, int] = {}
        for segment in self.segments:
            out[segment.kind] = out.get(segment.kind, 0) + segment.tokens
        return out


# ========================================================================================================================
# PROVIDER GROUND TRUTH
# ========================================================================================================================


def provider_usage(messages: list[BaseMessage]) -> ProviderUsage | None:
    """Read the latest model call's usage off the message history — the most recent ``AIMessage`` carrying
    ``usage_metadata`` reflects the current prompt size and cache split. Returns ``None`` when no such
    message exists yet (a thread before its first model response).

    Cache details ride in ``usage_metadata.input_token_details``; some provider integrations leave that
    unpopulated, so we fall back to the raw ``response_metadata.usage`` keys the Anthropic API returns.
    """
    for message in reversed(messages):
        meta = getattr(message, "usage_metadata", None)
        if not isinstance(message, AIMessage) or not meta:
            continue

        details = meta.get("input_token_details") or {}
        cache_read = details.get("cache_read")
        cache_creation = details.get("cache_creation")
        if cache_read is None and cache_creation is None:
            raw = (getattr(message, "response_metadata", None) or {}).get("usage", {})
            cache_read = raw.get("cache_read_input_tokens")
            cache_creation = raw.get("cache_creation_input_tokens")

        return ProviderUsage(
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            cache_read_tokens=cache_read or 0,
            cache_creation_tokens=cache_creation or 0,
        )
    return None


# ========================================================================================================================
# LOCAL ESTIMATION
# ========================================================================================================================
# Approximate, provider-neutral token counts via langchain's ``count_tokens_approximately`` — the absolute
# values are rough, which is fine because ``normalize`` rescales them to the provider's reported total.


def estimate_message_tokens(message: BaseMessage) -> int:
    """Approximate token cost of one message (content + role + any tool calls / tool_call_id)."""
    return count_tokens_approximately([message])


def estimate_system_tokens(system_prompt: str | None) -> int:
    """Approximate token cost of the system prompt, ``0`` when there is none. Counted as a ``SystemMessage``
    so the role overhead is included, matching how it rides the wire."""
    return count_tokens_approximately([SystemMessage(content=system_prompt)]) if system_prompt else 0


def estimate_tool_tokens(tools: list | None) -> int:
    """Approximate token cost of the tool *definitions* sent alongside the prompt — the slice the legacy
    breakdown missed. ``count_tokens_approximately`` stringifies each tool schema; it accepts both
    ``BaseTool`` objects and raw wire dicts (the Anthropic server-side web tools), so no per-kind
    handling is needed here."""
    return count_tokens_approximately([], tools=tools) if tools else 0


def normalize(segments: list[UsageSegment], target: int | None) -> list[UsageSegment]:
    """Scale ``segments`` so their token counts sum to ``target`` (the provider's reported ``input_tokens``),
    preserving each segment's share of the rough estimate. Returns the segments unchanged when ``target`` is
    unknown (no model call yet) or when there is nothing to scale against, so a pre-call report still shows
    raw estimates.

    Rounding is done on the running cumulative total rather than per segment, so the parts sum to ``target``
    *exactly* (no leftover drift) while each segment stays non-negative."""
    raw_total = sum(s.tokens for s in segments)
    if not target or not raw_total:
        return segments

    out: list[UsageSegment] = []
    cumulative_raw = 0
    assigned = 0
    for s in segments:
        cumulative_raw += s.tokens
        running = round(cumulative_raw * target / raw_total)
        out.append(UsageSegment(s.kind, running - assigned, message_id=s.message_id, label=s.label))
        assigned = running
    return out


# ========================================================================================================================
# CONTEXT WINDOW
# ========================================================================================================================


def compute_chat_model_max_tokens(chat_model: BaseChatModel) -> int | None:
    """Derive a model's total context window from its langchain ``profile``, or ``None`` when the necessary
    fields are missing. Profiles are a beta feature, so every step guards against absent/partial data."""
    profile = getattr(chat_model, "profile", None)
    if not profile:
        return None
    max_input = profile.get("max_input_tokens")
    if max_input is None:
        return None
    return max_input + profile.get("max_output_tokens", 0)


def countable(messages: list[BaseMessage]) -> list[BaseMessage]:
    """The messages that contribute to the prompt — everything except ``RemoveMessage`` deletion markers,
    which carry no wire content."""
    return [m for m in messages if not isinstance(m, RemoveMessage)]


def tool_kind(message: BaseMessage) -> str | None:
    """Whether a message is a tool *use* or a tool *result* — the two halves of a tool round-trip, which
    langchain splits across message types: the invocation rides on an ``AIMessage`` (``tool_calls`` plus a
    ``tool_use`` content block), and the result is a ``ToolMessage``. Returns ``None`` for messages that are
    neither (plain text turns, system notices)."""
    if isinstance(message, AIMessage) and message.tool_calls:
        return "use"
    if isinstance(message, ToolMessage):
        return "result"
    return None
