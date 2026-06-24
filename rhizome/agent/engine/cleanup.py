"""Message reclamation: marking messages reclaimable, and reclaiming them on request.

Two halves of the message-lifetime machinery live here (see ``engine.base.PromptEngine`` for the
overview), above ``metadata`` (the tag schema) and below ``state`` and the engines (which drive it):

- *Marking* — ``mark_reclaimable`` tags a message ``semi-permanent`` and bakes the inline marker. The
  auto-tagger calls it on bulky tool results; a tool may call it on its own result to self-tag.
- *Reclaiming* — ``apply_cleanup`` is the engine's SOLE emitter of cleanup edits. It consumes the
  ``CleanupRequest``s drained from ``BaseAgentState.pending_cleanups``, resolves eligibility and strategy,
  and returns in-place content replacements (same id, so ``add_messages`` replaces) promoted to
  ``permanent`` — a settled stub.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import BaseMessage

from rhizome.logs import get_logger

# The request vocabulary (``CleanupRequest``, ``Strategy``, the ``accumulate_cleanups`` reducer) is a leaf
# in ``base`` — what a requester speaks. This module is the machinery that resolves and applies it.
from ..base import CleanupRequest, HydrateRequest, Strategy
from .metadata import (
    detached_kwargs,
    expire_after_of,
    group_of,
    hydrations_of,
    is_reclaim_ineligible,
    lifetime_of,
    role_of,
    set_expire_after,
    set_group,
    set_hydrations,
    set_lifetime,
    set_reclaim_ineligible,
    strategy_of,
)

_logger = get_logger("agent.cleanup")


# ========================================================================================================================
# MARKING (identification)
# ========================================================================================================================


def _marker(group: str | None) -> str:
    """The static inline marker appended to a reclaimable message. Group-labelled so the agent reads, in
    place, the handle it would pass to ``cleanup_context``. Static (no volatile fields) so it costs the
    cache one reprice and nothing after."""
    return f"\n\n[reclaimable · {group}]" if group else "\n\n[reclaimable]"


def mark_reclaimable(
    message: BaseMessage, group: str | None = None, *, expire_after: int | None = None
) -> BaseMessage:
    """Tag ``message`` ``semi-permanent`` (and its ``group``) and append the inline marker, as a COPY
    (same id, so a re-emit replaces it in place). Idempotent — an already-semi-permanent message is
    returned untouched. The one identification primitive: the auto-tagger applies it to matching tool
    results, a tool may apply it to its own result to self-tag. Non-string content is tagged but unmarked.

    ``expire_after`` bakes a per-message auto-expiry age (user turns) that overrides the engine default —
    the lower-level control a self-tagging tool reaches for when its result should live longer/shorter than
    the norm. ``None`` leaves the message on the engine default.
    """
    if lifetime_of(message) == "semi-permanent":
        return message
    content = message.content
    if isinstance(content, str):
        content = content + _marker(group)
    copy = message.model_copy(update={"content": content, "additional_kwargs": detached_kwargs(message)})
    set_lifetime(copy, "semi-permanent")
    if group is not None:
        set_group(copy, group)
    if expire_after is not None:
        set_expire_after(copy, expire_after)
    return copy


def mark_reclaim_ineligible(message: BaseMessage) -> BaseMessage:
    """Stamp ``message`` as evaluated-and-passed-over by the auto-tagger, as a COPY (same id, so a re-emit
    replaces it in place). Idempotent. The flag rides in metadata only — never on the wire — so the copy is
    byte-identical to the provider and costs the prompt cache nothing; it just stops every future
    ``_identify`` pass from re-sizing this message, and freezes the decision so a later threshold/whitelist
    change can't retroactively reclaim it (which would shift the ``semi_perm`` breakpoint and break the
    prefix). Content untouched (no marker — an ineligible message advertises nothing)."""
    if is_reclaim_ineligible(message):
        return message
    copy = message.model_copy(update={"additional_kwargs": detached_kwargs(message)})
    return set_reclaim_ineligible(copy)


def promote(message: BaseMessage) -> BaseMessage:
    """Freeze a semi-permanent message to ``permanent`` WITHOUT touching content — used at a branch so the
    child inherits a byte-identical message (the cache spine) that is simply no longer reclaimable. Also
    stamps it ``reclaim_ineligible`` (an off-wire flag, so the spine stays byte-identical) so the auto-tagger
    never re-tags this settled inherited result on a later compile — without it, a still-bulky frozen result
    would be re-marked ``semi-permanent`` every compile, undoing the freeze. The stale inline marker rides
    along in the content on purpose: the cache prefix matters more. A no-op on a non-semi-permanent message."""
    if lifetime_of(message) != "semi-permanent":
        return message
    copy = message.model_copy(update={"additional_kwargs": detached_kwargs(message)})
    set_lifetime(copy, "permanent")
    return set_reclaim_ineligible(copy)


# ========================================================================================================================
# RECLAIMING (cleanup)
# ========================================================================================================================

DEFAULT_STRATEGY: Strategy = "stub"

STUB_CONTENT = "[Earlier content was cleared to reclaim context.]"

SUMMARY_PREFIX = "[Summarized to reclaim context]\n\n"

# Produces summaries for the ``summarize``-strategy messages of one cleanup pass: maps the target messages
# to ``{message id -> summary text}``. The engine injects it (the root engine runs a summarizer subagent);
# a target it omits — no summarizer wired, or a failed call — falls back to a stub.
type Summarizer = Callable[[list[BaseMessage]], Awaitable[dict[str, str]]]


async def apply_cleanup(
    messages: list[BaseMessage],
    requests: list[CleanupRequest] = (),
    *,
    expire_after: int | None = None,
    default_strategy: Strategy = DEFAULT_STRATEGY,
    summarize: Summarizer | None = None,
) -> list[BaseMessage]:
    """The engine's sole emitter of cleanup edits. Schedules ``semi-permanent`` messages for reclamation
    from two sources — explicit group ``requests`` and age expiry (a message past its effective expiry age)
    — then reclaims each once (dedup by id) by its effective strategy. Eligibility is resolved HERE, so a
    since-pinned/``permanent`` message is simply skipped. Strategy precedence is message > request >
    ``default_strategy`` (expiry has no request, so message > default). The expiry age is itself resolved
    message > engine default: ``expire_after`` is the engine default applied to a message that declares no
    age of its own; a message with neither is never auto-expired (explicit requests still reclaim it).

    A ``stub`` reclamation is a static placeholder; a ``summarize`` one replaces the content with a generated
    summary from the injected ``summarize`` callable (run once over all summarize-strategy targets, so the
    engine can batch/parallelize them). Without a summarizer, or for a target it fails, a ``summarize``
    strategy falls back to stub. Either way the reclaimed message is promoted to ``permanent`` (advancing the
    semi-permanent span) and re-emitted under its own id, so ``add_messages`` replaces in place."""
    scheduled: dict[str, tuple[BaseMessage, Strategy]] = {}
    for request in requests:
        group = request.get("group")
        for m in messages:
            if _schedulable(m, scheduled) and group_of(m) == group:
                scheduled[m.id] = (m, _effective_strategy(m, request, default_strategy))
    after = _user_counts_after(messages)
    for i, m in enumerate(messages):
        if not _schedulable(m, scheduled):
            continue
        threshold = expire_after_of(m)
        threshold = expire_after if threshold is None else threshold
        if threshold is not None and after[i] >= threshold:
            scheduled[m.id] = (m, strategy_of(m) or default_strategy)

    summaries: dict[str, str] = {}
    if summarize is not None:
        targets = [m for m, strategy in scheduled.values() if _is_summarize(strategy)]
        if targets:
            summaries = await summarize(targets)
    return [_reclaim(m, strategy, summaries.get(m.id)) for m, strategy in scheduled.values()]


def _schedulable(message: BaseMessage, scheduled: dict[str, Any]) -> bool:
    """A message eligible to be reclaimed this pass: not-yet-scheduled, identified, and semi-permanent."""
    return message.id is not None and message.id not in scheduled and lifetime_of(message) == "semi-permanent"


# ----- hydration (keep longer) ----- #

def apply_hydrations(
    messages: list[BaseMessage],
    requests: list[HydrateRequest],
    *,
    default_expiry: int | None,
    bump: int,
    max_hydrations: int,
) -> list[BaseMessage]:
    """The keep-it-longer mirror of ``apply_cleanup``: for each semi-permanent message in a requested
    ``group``, push its expiry out by ``bump`` user turns (baking a per-message ``expire_after`` that
    overrides the live default) and count the hydration — UNLESS it has already been hydrated
    ``max_hydrations`` times, in which case it is promoted to ``permanent`` (the agent has insisted enough;
    settle it, the freeze fallback). Re-emits share ids, so ``add_messages`` replaces in place; each message
    is hydrated at most once per pass (dedup by id)."""
    groups = {r.get("group") for r in requests}
    edits: dict[str, BaseMessage] = {}
    for m in messages:
        if not _schedulable(m, edits) or group_of(m) not in groups:
            continue
        if hydrations_of(m) + 1 >= max_hydrations:
            edits[m.id] = promote(m)                                  # hydrated enough -> settle permanent
        else:
            base = expire_after_of(m)
            base = default_expiry if base is None else base
            copy = m.model_copy(update={"additional_kwargs": detached_kwargs(m)})
            set_expire_after(copy, (base or 0) + bump)
            set_hydrations(copy, hydrations_of(m) + 1)
            edits[m.id] = copy
    return list(edits.values())


# ----- status (the agent's reclaimable-context view) ----- #

def reclamation_status(
    messages: list[BaseMessage], default_expiry: int | None
) -> dict[str, tuple[int, int | None]]:
    """Per-group summary of staged (semi-permanent) messages: ``group -> (count, min turns until expiry)``.
    Turns-left is each message's effective expiry (own ``expire_after`` else ``default_expiry``) minus its
    age in user turns; the minimum over the group is what the reminder shows (the soonest cleanup). ``None``
    turns-left means no expiry applies (neither a per-message age nor an engine default). Pure — the engine
    renders it into the tail reminder each ``prepare``."""
    after = _user_counts_after(messages)
    lefts: dict[str, list[int | None]] = {}
    for i, m in enumerate(messages):
        if lifetime_of(m) != "semi-permanent":
            continue
        threshold = expire_after_of(m)
        threshold = default_expiry if threshold is None else threshold
        turns_left = None if threshold is None else threshold - after[i]
        lefts.setdefault(group_of(m) or "(ungrouped)", []).append(turns_left)
    summary: dict[str, tuple[int, int | None]] = {}
    for group, values in lefts.items():
        finite = [v for v in values if v is not None]
        summary[group] = (len(values), min(finite) if finite else None)
    return summary


def _user_counts_after(messages: list[BaseMessage]) -> list[int]:
    """For each index, the number of genuine ``user`` messages strictly after it — the age (in user
    turns) a semi-permanent message at that position has accrued."""
    after = [0] * len(messages)
    running = 0
    for i in range(len(messages) - 1, -1, -1):
        after[i] = running
        if role_of(messages[i]) == "user":
            running += 1
    return after


def _effective_strategy(message: BaseMessage, request: CleanupRequest, default: Strategy) -> Strategy:
    """Resolve the strategy for one reclamation: the message's own tag wins, then the request's, then the
    engine default."""
    return strategy_of(message) or request.get("strategy") or default


def _is_summarize(strategy: Strategy) -> bool:
    """Whether ``strategy`` calls for a generated summary (vs a static stub). The ``+store`` variant's
    archival half is deferred; it summarizes like the bare form for now."""
    return strategy in ("summarize", "summarize+store")


def _reclaim(message: BaseMessage, strategy: Strategy, summary: str | None = None) -> BaseMessage:
    """Apply one strategy to one message. A ``summarize`` strategy with a produced ``summary`` keeps a
    condensed form; everything else — a ``stub`` strategy, or a ``summarize`` whose summarizer was absent or
    failed — falls back to the static stub."""
    if summary is not None:
        return _summarize(message, summary)
    if _is_summarize(strategy):
        _logger.debug("no summary produced for %s (strategy %r); stubbing", message.id, strategy)
    return _stub(message)


def _stub(message: BaseMessage) -> BaseMessage:
    """Replace ``message``'s content with the static stub placeholder and promote it to ``permanent`` (a
    settled stub), stamped ``reclaim_ineligible`` so the auto-tagger leaves it be. Re-emitted as a copy with
    the same id (and ``tool_call_id`` etc.), so it replaces in place and keeps tool adjacency. Replacing the
    content drops the reclaimable marker for free."""
    copy = message.model_copy(update={"content": STUB_CONTENT, "additional_kwargs": detached_kwargs(message)})
    set_lifetime(copy, "permanent")
    return set_reclaim_ineligible(copy)


def _summarize(message: BaseMessage, summary: str) -> BaseMessage:
    """Replace ``message``'s content with a generated ``summary`` (prefixed so the agent reads it as a
    condensed stand-in, not the original) and settle it: ``permanent`` + ``reclaim_ineligible``. The same
    in-place re-emit as ``_stub`` (shared id, tool adjacency), just retaining useful content."""
    copy = message.model_copy(update={
        "content": f"{SUMMARY_PREFIX}{summary}", "additional_kwargs": detached_kwargs(message),
    })
    set_lifetime(copy, "permanent")
    return set_reclaim_ineligible(copy)
