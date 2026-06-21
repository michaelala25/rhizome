"""The prompt engine: turning durable conversation state into the concrete model request, and the
input vocabulary (payloads) a session feeds it.

INVARIANT — **nothing under ``engine/`` imports from ``state``.** State reaches *down* into the engine
(``state`` pulls ``ConsumedResources`` and the cleanup request types from here), never the reverse.
Keeping every module in this package free of a ``..state`` import is exactly what lets this ``__init__``
eagerly re-export the public surface without an import cycle: pulling any engine submodule never re-enters
``state`` mid-load. Add a ``from ..state import`` to any engine module and that cycle comes back — so the
resource/state snapshot types that ``state`` needs live in ``engine.resources``, not the other way round.
"""

from .base import PromptCompilerMiddleware, PromptEngine
from .cleanup import accumulate_cleanups, apply_cleanup, CleanupRequest, mark_reclaimable, promote
from .payload import AgentPayload, MessagePayload, PayloadQueue, StateUpdatePayload
from .resources import ConsumedResources
from .root import RootPromptEngine

__all__ = [
    # engines + middleware
    "PromptEngine",
    "PromptCompilerMiddleware",
    "RootPromptEngine",
    # input vocabulary
    "AgentPayload",
    "MessagePayload",
    "StateUpdatePayload",
    "PayloadQueue",
    # cleanup (request types + marking the engine consumes)
    "CleanupRequest",
    "accumulate_cleanups",
    "apply_cleanup",
    "mark_reclaimable",
    "promote",
    # resource-context snapshot type state records
    "ConsumedResources",
]
