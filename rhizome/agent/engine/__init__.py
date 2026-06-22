"""The prompt engine: turning durable conversation state into the concrete model request.

INVARIANT — **nothing under ``engine/`` imports from ``state``.** State reaches *down* into the engine
package's dependencies, never the reverse. The small leaf types both sides share — the payload input
vocabulary, the cleanup-request types, and the ``ConsumedResources`` snapshot — live one level below in
``rhizome.agent.base``; ``state`` pulls them straight from there, and the engine pulls them from there
too (re-exporting them below for back-compat, so ``from .engine import MessagePayload`` still resolves).
Keeping every engine module free of a ``..state`` import is what lets this ``__init__`` eagerly re-export
its public surface without an import cycle.
"""

from ..base import (
    AgentPayload,
    accumulate_cleanups,
    CleanupRequest,
    ConsumedResources,
    MessagePayload,
    PayloadQueue,
    StateUpdatePayload,
)
from .base import PromptCompilerMiddleware, PromptEngine
from .cleanup import apply_cleanup, mark_reclaimable, promote
from .root import RootPromptEngine
from .usage import (
    compute_chat_model_max_tokens,
    provider_usage,
    ProviderUsage,
    UsageReport,
    UsageSegment,
)

__all__ = [
    # engines + middleware
    "PromptEngine",
    "PromptCompilerMiddleware",
    "RootPromptEngine",
    # token-usage accounting (engine.report returns a UsageReport; the app consumes it)
    "UsageReport",
    "UsageSegment",
    "ProviderUsage",
    "provider_usage",
    "compute_chat_model_max_tokens",
    # input vocabulary (defined in ``base``, re-exported here)
    "AgentPayload",
    "MessagePayload",
    "StateUpdatePayload",
    "PayloadQueue",
    # cleanup (request types from ``base``; marking/applying machinery from this package)
    "CleanupRequest",
    "accumulate_cleanups",
    "apply_cleanup",
    "mark_reclaimable",
    "promote",
    # resource-context snapshot type state records (defined in ``base``)
    "ConsumedResources",
]
