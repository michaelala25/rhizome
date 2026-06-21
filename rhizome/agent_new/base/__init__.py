"""Leaf vocabulary shared across the agent stack — the bottom of the dependency graph.

These are the small, dependency-light types that ``state``, ``tools``, and ``engine`` all speak: the input
payload vocabulary a session feeds the engine, the declarative cleanup-request types that ride on graph
state, and the resource-consumption snapshot type. They live here, below every subsystem, so any of them
can use the vocabulary without reaching "up" into another — and without the import cycles that caused.

INVARIANT — **nothing in this package imports from elsewhere in ``rhizome.agent_new``.** That is what
makes it a true leaf: ``engine``, ``state``, and ``tools`` may all import from ``base``, never the reverse.
"""

from .cleanup import accumulate_cleanups, CleanupRequest, Strategy
from .payload import AgentPayload, MessagePayload, PayloadQueue, StateUpdatePayload
from .resources import ConsumedResources

__all__ = [
    # input vocabulary
    "AgentPayload",
    "MessagePayload",
    "PayloadQueue",
    "StateUpdatePayload",
    # cleanup request vocabulary
    "Strategy",
    "CleanupRequest",
    "accumulate_cleanups",
    # resource-consumption snapshot
    "ConsumedResources",
]
