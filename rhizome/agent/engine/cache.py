"""Anthropic prompt-cache breakpoints: the generic mechanic the root engine's ``prepare`` uses to turn a
laid-out message list into a *cached* request. Engine-agnostic by construction — the placement *policy*
(which message is the head / branch / tail boundary) lives on the concrete engine; this module owns only
the *mechanism* and the *budget*.

Two pieces:

- **``apply_cache_control``** — return a COPY of a message carrying a ``cache_control`` breakpoint on its
  last content block (a plain ``str`` body is wrapped into a one-block text list). Copy, never mutate:
  ``prepare`` is view-only and the message objects are shared with checkpointed state.
- **``allocate``** — walk an ordered list of breakpoint CANDIDATES, each stamping its OWN ``cache_control``,
  spending a fixed budget. Anthropic caps a request at four ``cache_control`` breakpoints, and reads the
  *longest* cached prefix among the breakpoints that still match — so the design question is purely *which
  boundaries earn the scarce slots*. Here, **priority IS list order and the budget IS the integer**: the
  allocator places candidates top-to-bottom until the budget runs out, so the lowest-priority candidate is
  the one dropped when more apply than fit. A candidate that does not apply resolves to ``None``; two
  candidates resolving onto the same message place a single breakpoint (dedupe). Because the descriptor
  rides on the candidate, one pass can mix TTLs — a long-lived TTL on a stable prefix anchor, a short one
  on the volatile tail. A candidate may resolve to a LIST of targets (placed in its OWN order, greedily,
  within the remaining budget) — that is how one priority level fans out across several boundaries (the
  root engine's ancestor breakpoints).

The one mechanic worth holding in mind: Anthropic caches the prefix UP TO AND INCLUDING the block a
breakpoint sits on. So a candidate that wants a prefix to end *before* some message targets the message
just before it — that is how the root engine's branch-leaf candidate keeps a node-specific marker OUT of
the shared prefix (see ``RootPromptEngine``).
"""

from dataclasses import dataclass
from typing import Callable, Protocol

from langchain_core.messages import BaseMessage

# Anthropic accepts at most four cache_control breakpoints per request.
MAX_BREAKPOINTS = 4

CacheControl = dict[str, str]


class OptionReader(Protocol):
    """The slice of an option handle the engine reads each turn — structurally an ``OptionRef[str]``. Kept
    local so nothing under ``engine/`` depends on the app's options module."""

    def get(self) -> str: ...


# ========================================================================================================================
# CACHE-CONTROL DESCRIPTORS
# ========================================================================================================================


def cache_control(ttl: str) -> CacheControl:
    """The ``cache_control`` descriptor for a ``prompt_cache_ttl`` option value (``5m`` | ``1h``). The TTL
    is emitted explicitly so a dump reads the lifetime back; ``5m`` is also Anthropic's default."""
    return {"type": "ephemeral", "ttl": ttl}


def cache_control_of(message: BaseMessage) -> CacheControl | None:
    """The ``cache_control`` carried on ``message``'s last content block, or ``None`` — the read side the
    debug dump uses to show where breakpoints landed."""
    content = message.content
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        cc = content[-1].get("cache_control")
        return cc if isinstance(cc, dict) else None
    return None


def apply_cache_control(message: BaseMessage, cc: CacheControl) -> BaseMessage | None:
    """Return a COPY of ``message`` carrying ``cc`` on its last content block; ``None`` when there is no
    block to annotate (empty content). A ``str`` body becomes a single text block; a list body has its last
    block annotated in place (a bare string element is promoted to a text block first).

    Never mutates ``message``: only ``content`` is replaced, with a freshly built list, so the original
    (still living in checkpointed state) is untouched and its ``additional_kwargs`` / id ride along on the
    copy. Targets are text-bearing by construction (resource blocks, markers, conversation turns, tool
    results); finer block-eligibility handling is deferred."""
    content = message.content
    if isinstance(content, str) and content:
        new_content: list = [{"type": "text", "text": content, "cache_control": cc}]
    elif isinstance(content, list) and content:
        new_content = list(content)
        last = new_content[-1]
        new_content[-1] = (
            {**last, "cache_control": cc} if isinstance(last, dict)
            else {"type": "text", "text": str(last), "cache_control": cc}
        )
    else:
        return None
    return message.model_copy(update={"content": new_content})


# ========================================================================================================================
# BREAKPOINT ALLOCATION
# ========================================================================================================================


@dataclass(frozen=True)
class Breakpoint:
    """One breakpoint candidate: a ``name`` (for dumps / docs), a ``resolve`` that returns the message(s) it
    wants the breakpoint placed ON — a single message, a LIST (placed in its own order, greedily), or
    ``None`` when it does not apply — and the ``cache_control`` descriptor to stamp there. PRIORITY is the
    candidate's position in the list handed to ``allocate`` — higher means placed first, and dropped last
    under budget pressure. Carrying the descriptor per candidate is what lets one ``allocate`` pass mix TTLs
    across positions."""

    name: str
    resolve: Callable[[list[BaseMessage]], BaseMessage | list[BaseMessage] | None]
    cache_control: CacheControl


def allocate(
    messages: list[BaseMessage], candidates: list[Breakpoint], budget: int = MAX_BREAKPOINTS
) -> list[BaseMessage]:
    """Place up to ``budget`` cache breakpoints on ``messages``, walking ``candidates`` in priority order
    and, within a candidate that yields several targets, those in their own order — greedily. Each
    breakpoint stamps its candidate's ``cache_control``.

    Skips a target that is already broken (dedupe — the higher-priority placement wins and the slot is not
    spent twice) or has no annotatable content. Returns ``messages`` UNCHANGED (same object) when nothing
    was placed, so a caller can preserve request identity; otherwise a new list with the annotated copies
    swapped in."""
    by_identity = {id(m): i for i, m in enumerate(messages)}
    placed: dict[int, BaseMessage] = {}
    for candidate in candidates:
        if len(placed) >= budget:
            break
        result = candidate.resolve(messages)
        targets = result if isinstance(result, list) else [] if result is None else [result]
        for target in targets:
            if len(placed) >= budget:
                break
            index = by_identity.get(id(target))
            if index is None or index in placed:
                continue
            annotated = apply_cache_control(target, candidate.cache_control)
            if annotated is not None:
                placed[index] = annotated

    if not placed:
        return messages
    return [placed.get(i, m) for i, m in enumerate(messages)]
