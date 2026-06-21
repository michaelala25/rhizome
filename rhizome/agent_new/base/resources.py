"""The resource-consumption snapshot type.

``ConsumedResources`` records, per channel, which resource nodes a thread has already ingested into its
prompt context — the baseline the engine diffs the desired load state against. It rides on
``RootAgentState.consumed_resource_context``, so it lives at the leaf (importing only ``resources_new``)
rather than in the engine: that lets ``state`` carry it without importing the engine. The machinery that
computes the per-channel deltas and writes the snapshot back lives in ``engine.resources``.
"""

from typing import TypedDict

from rhizome.resources_new import ResourceTreeNode

# Functional syntax because "global" is a Python keyword. Channel-split on purpose: the prompt engine's
# deltas edit different context messages — one shared global message, one per-node local message — so
# consumption must be tracked per channel.
ConsumedResources = TypedDict(
    "ConsumedResources",
    {"global": list[ResourceTreeNode], "local": list[ResourceTreeNode]},
)
