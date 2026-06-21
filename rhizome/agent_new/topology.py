"""Topology snapshot: a content-light, immutable picture of the agent graph's shape.

``AgentGraph`` publishes a fresh ``TopologySnapshot`` into a shared ``TopologyView`` cell on every
topology change (root / branch / merge), atomic-swap style so a reader never sees a half-built graph. The
conversation layer wires that one cell — plus each node's own id — into the node's compile context
(``ConversationGraph._make_node``), so a prompt engine can witness the *whole, current* graph at compile
time: its own lineage, the branch points along it, sibling and leaf structure. Pure topology — nodes,
edges, frozen flags, display names — nothing checkpointed, nothing provider-specific. It is a pull handle,
never agent state.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NodeInfo:
    """One node's place in the graph. ``id`` is the stable ``AgentNode.id``; ``parents`` has more than one
    entry only for a merged node. ``name`` is opportunistic display metadata — ``None`` on bare agent
    nodes, present on conversation nodes — so a marker can name where a branch forked from."""

    id: int
    parents: tuple[int, ...] = ()
    children: tuple[int, ...] = ()
    frozen: bool = False
    name: str | None = None


@dataclass(frozen=True)
class TopologySnapshot:
    """An immutable picture of the graph at one instant, keyed by node id. (Holds a dict, so it is not
    hashable — rebuilt wholesale, never compared or set-stored.)"""

    nodes: dict[int, NodeInfo] = field(default_factory=dict)

    def node(self, node_id: int | None) -> NodeInfo | None:
        return self.nodes.get(node_id) if node_id is not None else None

    def is_leaf(self, node_id: int) -> bool:
        info = self.nodes.get(node_id)
        return info is not None and not info.children


class TopologyView:
    """A live cell holding the current ``TopologySnapshot``. One instance per graph, shared by every
    node's context; the graph ``publish``es a new snapshot on each topology change and readers see a
    consistent one through ``snapshot``. Read-only for everyone but the graph."""

    def __init__(self) -> None:
        self._snapshot = TopologySnapshot()

    @property
    def snapshot(self) -> TopologySnapshot:
        return self._snapshot

    def publish(self, snapshot: TopologySnapshot) -> None:
        self._snapshot = snapshot
