"""Declarative context-cleanup vocabulary: the reclaim ``Strategy``, the ``CleanupRequest`` filed onto
graph state, and the reducer that accumulates those requests.

These are the leaf types of the cleanup system — what a *requester* (a tool, an app hook, the agent's
``cleanup_context``) speaks. The machinery that resolves and applies them lives in ``engine.cleanup``; it
imports these types from here. Keeping the request vocabulary at the leaf is what lets ``state`` carry a
``list[CleanupRequest]`` on ``pending_cleanups`` without importing the engine.
"""

from typing import Literal, NotRequired, TypedDict

Strategy = Literal["stub", "stub+store", "summarize", "summarize+store"]
"""How a reclaimed message's content is replaced — two axes under one name: a *transform* (``stub`` swaps
a placeholder, ``summarize`` swaps a generated summary) and whether the original is *stored* for retrieval
(``+store``). Only ``stub`` is built today; the rest name the space. Resolved message > request > engine
default (a message may declare its own via ``set_strategy``)."""


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
