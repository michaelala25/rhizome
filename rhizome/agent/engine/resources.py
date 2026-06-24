"""Resource-context machinery for the root prompt engine: the per-channel load deltas, the well-known
message-id scheme for resource/index context blocks, and the tree-grouping and block-building helpers.

The consumed-snapshot *type* (``ConsumedResources``) lives at the leaf in ``base.resources`` — that is what
lets ``state`` carry it without importing the engine; this module imports it back to compute deltas against
it. ``RootPromptEngine`` (in ``engine.root``) drives all of this: it diffs the stores against a thread's
consumed snapshot and emits the context messages, anchoring each block by the ids minted here.
"""

from typing import Iterable, TYPE_CHECKING

from langchain_core.messages import BaseMessage

from rhizome.resources import (
    build_index_block,
    build_resource_block,
    load_delta,
    ResourceLoadDelta,
    ResourceTree,
    ResourceTreeNode,
)

# The consumed-snapshot type is leaf vocab in ``base`` (it rides on graph state); this module is the
# machinery that diffs against it and writes it back.
from ..base import ConsumedResources

if TYPE_CHECKING:
    from ..context import RootAgentContext


# ========================================================================================================================
# CONSUMED SNAPSHOT DELTAS
# ========================================================================================================================
# ``ConsumedResources`` itself lives in ``base.resources``; the per-channel delta computation lives here.


def resource_deltas(
    consumed: ConsumedResources | None,
    ctx: "RootAgentContext",
) -> tuple[ResourceLoadDelta, ResourceLoadDelta, ConsumedResources]:
    """Per-channel deltas between this thread's consumed snapshot and the stores' desired state, plus
    the fresh snapshot to persist back to ``RootAgentState.consumed_resource_context``.

    Returns ``(global_delta, local_delta, snapshot)``. An engine's resource pass applies the deltas
    as message edits — fetch content for additions, drop blocks for removals, against the global and
    per-node local well-known message IDs respectively — and writes the snapshot into the SAME state
    update, so consumption becomes a fact exactly when the content lands.

    The effective local desire is the local store minus whatever the global store already covers —
    the concomitance backstop. It is entry-level skip-don't-fix: the stores are never mutated (the
    writer-side policy owns the invariant), double-injection is suppressed, and the suppression
    self-heals in both directions (a node skipped while globally covered re-enters the local delta
    the moment global coverage retreats, because it was never recorded as consumed). Partial subtree
    overlaps below entry granularity remain the writer's responsibility.
    """
    consumed_global = (consumed or {}).get("global") or []
    consumed_local = (consumed or {}).get("local") or []

    global_loaded = list(ctx.global_resources.loaded) if ctx.global_resources is not None else []
    local_loaded = list(ctx.local_resources.loaded) if ctx.local_resources is not None else []
    if ctx.global_resources is not None:
        local_loaded = [n for n in local_loaded if not ctx.global_resources.is_loaded(n)]

    return (
        load_delta(consumed_global, global_loaded),
        load_delta(consumed_local, local_loaded),
        ConsumedResources(**{"global": global_loaded, "local": local_loaded}),
    )


# ========================================================================================================================
# WELL-KNOWN MESSAGE IDS
# ========================================================================================================================

GLOBAL_RESOURCE_MESSAGE_PREFIX = "global-resource-ctx-"


def global_resource_message_id(resource_id: int) -> str:
    """Deterministic, thread-stable id for a resource's global context message — one per resource. The
    ``add_messages`` reducer replaces it in place on a content change and ``RemoveMessage`` drops it on
    unload; ``prepare`` locates the block by this prefix."""
    return f"{GLOBAL_RESOURCE_MESSAGE_PREFIX}{resource_id}"


def is_global_resource_message(message: BaseMessage) -> bool:
    return bool(message.id) and message.id.startswith(GLOBAL_RESOURCE_MESSAGE_PREFIX)


LOCAL_RESOURCE_MESSAGE_PREFIX = "local-resource-ctx-"


def local_resource_message_id(resource_id: int) -> str:
    """Per-resource id for a node-local context message. Keyed by resource (not node) so it inherits
    cleanly across a branch — the child manages the same id it was seeded, replace-in-place on a content
    change, ``RemoveMessage`` on unload — exactly like the global block but a distinct prefix and segment.
    The concomitance backstop in ``resource_deltas`` keeps a globally-covered resource out of this set, so
    the two channels never inject the same resource at once."""
    return f"{LOCAL_RESOURCE_MESSAGE_PREFIX}{resource_id}"


def is_local_resource_message(message: BaseMessage) -> bool:
    return bool(message.id) and message.id.startswith(LOCAL_RESOURCE_MESSAGE_PREFIX)


INDEX_RESOURCE_MESSAGE_ID = "index-resource-ctx"


def is_index_resource_message(message: BaseMessage) -> bool:
    """The vector index's "what's queryable" reminder — a SINGLE message (not one per resource, unlike
    the context channels), rebuilt in place on a load change by ``compile`` and floated to the ephemeral
    tail by ``prepare``."""
    return message.id == INDEX_RESOURCE_MESSAGE_ID


# ========================================================================================================================
# TREE GROUPING & BLOCK BUILDING
# ========================================================================================================================


def owning_resource_id(node: ResourceTreeNode, tree: ResourceTree) -> int | None:
    """Walk up to the ``("resource", rid)`` root that owns ``node`` — pure tree traversal, no DB. A
    resource node answers immediately (so a deleted resource still maps to its id); a section whose owner
    has left the tree yields ``None``."""
    current: ResourceTreeNode | None = node
    while current is not None:
        if current.kind == "resource":
            return current.id
        current = tree.parent(current)
    return None


def group_by_resource(
    nodes: Iterable[ResourceTreeNode], tree: ResourceTree
) -> dict[int, list[ResourceTreeNode]]:
    """Group a flat set of nodes by the ``("resource", rid)`` root that owns each — the by-resource
    shape the index listing wants. Nodes whose owner has left the tree are dropped."""
    grouped: dict[int, list[ResourceTreeNode]] = {}
    for node in nodes:
        rid = owning_resource_id(node, tree)
        if rid is not None:
            grouped.setdefault(rid, []).append(node)
    return grouped


def block_builder(session_factory, resource_id: int, nodes: list[ResourceTreeNode]):
    """A no-arg async thunk that opens its own session to build one resource's block. Handed to
    ``store.block`` so the DB read fires only on a cache miss — a warm global store serves branches
    without touching the DB."""
    async def build() -> str | None:
        async with session_factory() as session:
            return await build_resource_block(session, resource_id, nodes)
    return build


async def index_block(grouped: dict[int, list[ResourceTreeNode]], session_factory) -> str | None:
    """Open a session to render the index listing — mirrors ``block_builder`` but for the single index
    message, so the DB read fires only when the loaded set actually changed."""
    async with session_factory() as session:
        return await build_index_block(session, grouped)
