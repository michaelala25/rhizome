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

from typing import Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage

from rhizome.logs import get_logger

from .metadata import (
    detached_kwargs,
    group_of,
    lifetime_of,
    role_of,
    set_group,
    set_lifetime,
    Strategy,
    strategy_of,
)

_logger = get_logger("agent.cleanup")


# ========================================================================================================================
# REQUESTS
# ========================================================================================================================


class CleanupRequest(TypedDict):
    """A declarative request to reclaim a cleanup ``group``, filed onto ``BaseAgentState.pending_cleanups``
    by a tool / app hook / the agent's ``cleanup_context``. The engine's cleanup pass resolves it and is
    the sole emitter of the edits — a requester expresses intent, never the edit itself. A ``TypedDict``
    (not a dataclass) because it rides the checkpoint, where a dataclass would come back a bare dict."""

    group: str
    strategy: NotRequired[Strategy]
    """Request-level override; absent defers to each message's own tag, then the engine default."""
    reason: NotRequired[str]
    """Optional hint (e.g. summary guidance) for the strategies that consume one."""


def accumulate_cleanups(
    left: list[CleanupRequest] | None, right: list[CleanupRequest] | None
) -> list[CleanupRequest]:
    """Reducer for ``pending_cleanups``: append filed requests (parallel tools compose), with ``None`` as
    the drain signal the cleanup pass writes once it has consumed them — mirroring the ``None``-clears
    convention of ``state.merge_typeddict_field``."""
    if right is None:
        return []
    return (left or []) + right


# ========================================================================================================================
# MARKING (identification)
# ========================================================================================================================


def _marker(group: str | None) -> str:
    """The static inline marker appended to a reclaimable message. Group-labelled so the agent reads, in
    place, the handle it would pass to ``cleanup_context``. Static (no volatile fields) so it costs the
    cache one reprice and nothing after."""
    return f"\n\n[reclaimable · {group}]" if group else "\n\n[reclaimable]"


def mark_reclaimable(message: BaseMessage, group: str | None = None) -> BaseMessage:
    """Tag ``message`` ``semi-permanent`` (and its ``group``) and append the inline marker, as a COPY
    (same id, so a re-emit replaces it in place). Idempotent — an already-semi-permanent message is
    returned untouched. The one identification primitive: the auto-tagger applies it to matching tool
    results, a tool may apply it to its own result to self-tag. Non-string content is tagged but unmarked.
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
    return copy


def promote(message: BaseMessage) -> BaseMessage:
    """Freeze a semi-permanent message to ``permanent`` WITHOUT touching content — used at a branch so the
    child inherits a byte-identical message (the cache spine) that is simply no longer reclaimable. The
    stale inline marker rides along in the content on purpose: the cache prefix matters more. A no-op on a
    message that is not semi-permanent."""
    if lifetime_of(message) != "semi-permanent":
        return message
    copy = message.model_copy(update={"additional_kwargs": detached_kwargs(message)})
    return set_lifetime(copy, "permanent")


# ========================================================================================================================
# RECLAIMING (cleanup)
# ========================================================================================================================

DEFAULT_STRATEGY: Strategy = "stub"

STUB_CONTENT = "[Earlier content was cleared to reclaim context.]"


def apply_cleanup(
    messages: list[BaseMessage],
    requests: list[CleanupRequest] = (),
    *,
    expire_after: int | None = None,
    default_strategy: Strategy = DEFAULT_STRATEGY,
) -> list[BaseMessage]:
    """The engine's sole emitter of cleanup edits. Schedules ``semi-permanent`` messages for reclamation
    from two sources — explicit group ``requests`` and, when ``expire_after`` is set, age expiry (a
    message past ``expire_after`` genuine user turns) — then reclaims each once (dedup by id) by its
    effective strategy. Eligibility is resolved HERE, so a since-pinned/``permanent`` message is simply
    skipped. Strategy precedence is message > request > ``default_strategy`` (expiry has no request, so
    message > default). A reclaimed message is promoted to ``permanent`` (a settled stub), advancing the
    semi-permanent span; the re-emits share their ids, so ``add_messages`` replaces in place."""
    scheduled: dict[str, tuple[BaseMessage, Strategy]] = {}
    for request in requests:
        group = request.get("group")
        for m in messages:
            if _schedulable(m, scheduled) and group_of(m) == group:
                scheduled[m.id] = (m, _effective_strategy(m, request, default_strategy))
    if expire_after is not None:
        after = _user_counts_after(messages)
        for i, m in enumerate(messages):
            if _schedulable(m, scheduled) and after[i] >= expire_after:
                scheduled[m.id] = (m, strategy_of(m) or default_strategy)
    return [_reclaim(m, strategy) for m, strategy in scheduled.values()]


def _schedulable(message: BaseMessage, scheduled: dict[str, Any]) -> bool:
    """A message eligible to be reclaimed this pass: not-yet-scheduled, identified, and semi-permanent."""
    return message.id is not None and message.id not in scheduled and lifetime_of(message) == "semi-permanent"


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


def _reclaim(message: BaseMessage, strategy: Strategy) -> BaseMessage:
    """Apply one strategy to one message. Only ``stub`` is implemented; the rest name the space and fall
    back to ``stub`` (logged) until their transforms land."""
    if strategy != "stub":
        _logger.warning("cleanup strategy %r not implemented; falling back to stub", strategy)
    return _stub(message)


def _stub(message: BaseMessage) -> BaseMessage:
    """Replace ``message``'s content with the static stub placeholder and promote it to ``permanent`` (a
    settled stub). Re-emitted as a copy with the same id (and ``tool_call_id`` etc.), so it replaces in
    place and keeps tool adjacency. Replacing the content drops the reclaimable marker for free."""
    copy = message.model_copy(update={"content": STUB_CONTENT, "additional_kwargs": detached_kwargs(message)})
    return set_lifetime(copy, "permanent")
