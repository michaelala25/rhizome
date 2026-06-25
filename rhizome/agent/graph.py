"""Agent graph: a tree-with-merges of conversation nodes over a shared runtime.

``AgentNode`` is one conversation: an ``AgentSession`` (its own thread in the shared checkpointer) and a
worker slot for the in-flight stream task. ``AgentGraph`` owns the topology — a ``MergeTree`` of nodes —
and is the only layer that creates, freezes, branches, and merges them. Communication is keyed by
*cursor*: a root-to-node ``Path`` in the unfolding, so a node reached through a merge has one state but
separately addressable lineages.

The graph is *abstract*: it seeds a child's checkpoint from its parent(s) and tracks topology, but knows
nothing about what else a node carries. Per-node side channels (resource stores, ...) live on node
subclasses and ride across a branch/merge edge through ``AgentNode.derive`` — see
``rhizome.app.chat_area.conversation_graph`` for the conversation realization.

Lifecycle rules:

- The topology opens lazily: construction leaves the graph rootless; the owner calls ``make_root`` once
  everything is wired. Operations gate on the root existing.
- Only leaves talk. A node freezes when it gains children (branch or merge); frozen nodes are history,
  and their leaves carry the conversation forward.
- ``branch`` seeds the child's thread with a copy of the parent's checkpointed state. Message ids are
  preserved by the ``add_messages`` reducer, which keeps the child's prompt prefix byte-identical to the
  parent's (cache spine) and is what makes merge-by-union possible.
- ``merge`` (EXPERIMENTAL) creates a fresh child below both parents: ``into``'s state is the spine,
  ``from_``'s messages append as a deduped suffix. See ``AgentGraph.merge`` for caveats.
"""

import asyncio
from typing import Any, Callable, Coroutine, Iterator

from rhizome.utils.callbacks import CallbackHost
from rhizome.utils.data_structures import MergeTree, Path

from .runtime import AgentRuntime
from .session import AgentInstance, AgentPayload, AgentSession, InvokeResult
from .streaming import AgentStreamingContext
from .topology import NodeInfo, TopologySnapshot, TopologyView


WorkerScheduler = Callable[[Coroutine[Any, Any, None]], Any]
"""``asyncio.create_task``-shaped callable used to spawn stream workers; Textual's
``Widget.run_worker`` fits the same shape. The returned handle only needs ``.cancel()``."""

Cursor = Path
"""A root-to-node path addressing one location in the graph's unfolding. A plain alias on purpose:
cursors are ordinary ``Path`` values, so paths yielded by ``MergeTree`` traversals are valid cursors."""


class AgentNode:
    """One conversation in the graph.

    Owns the ``AgentSession`` (and through it the conversation thread) and the worker driving an in-flight
    ``stream``. Created only by ``AgentGraph.make_node`` — which stamps ``id`` and ``notify_busy_changed``
    onto the freshly built node — and frozen only by the graph's ``branch``/``merge``. Node-local side
    channels (resource stores, ...) belong to subclasses; ``derive`` carries them across a branch/merge
    edge.
    """

    id: int
    # Stamped by AgentGraph.make_node after construction — _make_node builds the node id-less.

    frozen: bool
    # Set by the graph when this node gains children. Frozen nodes refuse send/stream/invoke.

    notify_busy_changed: Callable[["AgentNode", bool], None] | None
    # Hook the owning graph installs (see AgentGraph.make_node): called with ``(node, busy)`` at the
    # exact pinpoints where ``busy`` flips for worker runs — right after the stream worker is scheduled,
    # and right after the worker slot clears in the worker's finally.

    def __init__(self, session: AgentSession, scheduler: WorkerScheduler) -> None:
        self.frozen = False
        self.notify_busy_changed = None
        self._session = session
        self._scheduler = scheduler
        self._worker: Any = None

    # Forwards from the underlying AgentSession
    @property
    def session(self) -> AgentSession:
        return self._session

    @property
    def thread_id(self) -> str:
        return self._session.thread_id

    @property
    def queued(self) -> list[AgentPayload]:
        return self._session.queued

    @property
    async def agent_state(self) -> dict[str, Any]:
        return await self._session.agent_state

    async def seed_state(self, values: dict[str, Any]) -> None:
        await self._session.seed_state(values)

    def acquire(self) -> AgentInstance:
        return self._session.acquire()

    @property
    def busy(self) -> bool:
        return self._worker is not None or self._session.busy

    # -------------------------------------------------------------
    # Derivation — node-local carry-forward across a branch/merge edge
    # -------------------------------------------------------------

    def derive(self, parent: "AgentNode", merged_from: "AgentNode | None" = None) -> None:
        """Carry node-local side channels from the parent(s) into this freshly-created child.

        No-op on the base node — it carries nothing beyond the checkpoint, which the graph seeds
        separately (and unconditionally, so correctness never rides on a subclass calling ``super``). A
        branch passes one ``parent``; a merge passes the spine parent plus ``merged_from``. Subclasses
        override to copy their own channels — see ``ConversationNode``.
        """

    # -------------------------------------------------------------
    # Communication — blocked once frozen
    # -------------------------------------------------------------

    def _require_live(self) -> None:
        if self.frozen:
            raise RuntimeError(f"AgentNode {self.id} is frozen (it has children); talk to a leaf instead")

    def send(self, payload: AgentPayload, eager: bool = False) -> None:
        self._require_live()
        self._session.send(payload, eager)

    def stream(self, stream_context: AgentStreamingContext, payloads: list[AgentPayload] | None = None) -> None:
        """Spawn a worker driving ``session.stream``. Fire-and-forget — completion, errors, and
        cancellation all land through the stream context's callbacks."""
        self._require_live()
        if self.busy:
            raise RuntimeError(f"AgentNode {self.id} already has a run in flight")

        async def _run() -> None:
            try:
                await self._session.stream(stream_context, payloads)
            finally:
                # The exact moment ``busy`` flips false — after the stream context's on_complete
                # has run (it fires inside session.stream's finally), so consumers hear "idle"
                # only once the run's teardown has settled.
                self._worker = None
                self._fire_busy_changed(False)

        self._worker = self._scheduler(_run())
        self._fire_busy_changed(True)

    async def invoke(self, payloads: list[AgentPayload] | None = None) -> InvokeResult:
        self._require_live()
        return await self._session.invoke(payloads)

    def cancel(self) -> None:
        """Cancel the in-flight stream worker, if any. Cooperative: ``busy`` stays True (and the
        busy-changed hook stays quiet) until the cancellation unwinds through the worker."""
        if self._worker is not None:
            self._worker.cancel()

    def _fire_busy_changed(self, busy: bool) -> None:
        if self.notify_busy_changed is not None:
            self.notify_busy_changed(self, busy)

    # Hash by id — required by the Graph API, stable across the node's lifetime
    def __hash__(self) -> int:
        return hash(self.id)


class AgentGraph[N: AgentNode](CallbackHost):
    """The conversation topology: nodes in a ``MergeTree``, dispatch keyed by cursor/node/id.

    Generic over the node type; ``_make_node`` is the subclass hook for producing richer nodes (e.g. a
    ``ConversationNode`` carrying a feed and resource stores). The topology opens lazily — construction
    leaves the graph rootless and ``make_root`` mints the root once the owner has finished wiring, so a
    subclass's own fields are all set before the first node is built.
    """

    class Callbacks:
        OnNodeBusyChanged = "OnNodeBusyChanged"
        """``(node, busy)`` — fired at the exact pinpoints where ``node.busy`` flips for worker
        runs: right after a stream worker is scheduled, and right after the worker slot clears
        (success, error, or cancellation unwind — in every case after the stream context's
        ``on_complete``). The payload always agrees with ``node.busy`` at emit time. ``invoke()``
        runs are await-shaped (the caller already knows) and do not fire this."""

        OnTopologyChanged = "OnTopologyChanged"
        """Nullary — fired after any topology mutation (root mint, branch, merge) republishes the
        snapshot; the single emit site is ``_publish_topology``. Subscribers re-read the graph
        (nodes / edges / frozen) to repaint; the prompt engine ignores it, pulling the live
        ``TopologyView`` snapshot at compile time instead."""

    def __init__(
        self,
        runtime: AgentRuntime,
        scheduler: WorkerScheduler = asyncio.create_task,
        *,
        agent_key: str = "root",
    ) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnNodeBusyChanged: (AgentNode, bool),
            self.Callbacks.OnTopologyChanged: None,
        })

        self._runtime = runtime
        self._scheduler = scheduler
        self._agent_key = agent_key

        self._next_node_id = 0
        self._nodes_by_id: dict[int, N] = {}
        self._tree: MergeTree[N] | None = None   # opened by make_root

        # Shared, live topology snapshot. The graph republishes it on every topology change; the
        # conversation layer wires this one cell into each node's context so the prompt engine can pull
        # the whole current graph at compile time. Never agent state.
        self._topology = TopologyView()

    # -------------------------------------------------------------
    # Topology lifecycle
    # -------------------------------------------------------------

    def make_root(self) -> Cursor[N]:
        """Mint the root node and open the topology; returns the root cursor.

        Call once, after construction, before any branch/merge/communication. Splitting this out of
        ``__init__`` keeps construction order-independent: a subclass finishes setting its own fields
        before ``_make_node`` first runs. Raises if the graph is already rooted.
        """
        if self._tree is not None:
            raise RuntimeError("graph is already rooted")
        self._tree = MergeTree(self.make_node())
        self._publish_topology()
        return self.root_cursor()

    def _require_root(self) -> MergeTree[N]:
        if self._tree is None:
            raise RuntimeError("graph has no root yet — call make_root() after construction")
        return self._tree

    @property
    def root(self) -> N:
        return self._require_root().root

    def root_cursor(self) -> Cursor[N]:
        return self._require_root().root_path()

    def leaves(self) -> Iterator[Cursor[N]]:
        """Every live conversation, as a full root-to-leaf cursor."""
        return self._require_root().leaf_paths()

    # -------------------------------------------------------------
    # Node creation & resolution
    # -------------------------------------------------------------

    def make_node(self) -> N:
        """Create, register, and wire a fresh node — ``id`` and the busy hook stamped on after the
        subclass builds it. The id is allocated *first* and passed into ``_make_node`` so a subclass can
        wire it into the session context; ``make_node`` then stamps it on the node too. The node is not
        yet attached to the topology; ``make_root``/``branch``/``merge`` add the edge."""
        node_id = self._next_node_id
        self._next_node_id += 1
        node = self._make_node(node_id)
        node.id = node_id
        node.notify_busy_changed = self._emit_busy_changed
        self._nodes_by_id[node_id] = node
        return node

    def _emit_busy_changed(self, node: AgentNode, busy: bool) -> None:
        self.emit(self.Callbacks.OnNodeBusyChanged, node, busy)

    def _make_node(self, node_id: int) -> N:
        """Subclass hook: build the node and its session. ``node_id`` is the id ``make_node`` will stamp,
        handed in so a subclass can wire it (and the shared ``TopologyView``) into the session context
        before the node is registered. The base ignores it — bare nodes carry no topology-aware context.
        Must not stamp ``id`` or register; ``make_node`` owns that."""
        return AgentNode(self._runtime.new(self._agent_key), self._scheduler)  # type: ignore[return-value] — base builds AgentNode

    def _build_topology_snapshot(self) -> TopologySnapshot:
        """A snapshot of the current topology, built from the live tree — ids, parent/child edges, the
        frozen flag, and (opportunistically) a node's display name."""
        tree = self._tree
        if tree is None:
            return TopologySnapshot()
        return TopologySnapshot(nodes={
            nid: NodeInfo(
                id=nid,
                parents=tuple(p.id for p in tree.parents(node)),
                children=tuple(c.id for c in tree.children(node)),
                frozen=node.frozen,
                name=getattr(node, "name", None),
            )
            for nid, node in self._nodes_by_id.items()
        })

    def _publish_topology(self) -> None:
        """Rebuild and publish the topology snapshot, then fire ``OnTopologyChanged`` — called wherever
        the topology changes (root / branch / merge). Snapshot readers pick it up on their next compile
        through the context's ``TopologyView``; callback subscribers (e.g. a graph visualizer) repaint
        now. No per-node state is written."""
        self._topology.publish(self._build_topology_snapshot())
        self.emit(self.Callbacks.OnTopologyChanged)

    def node(self, at: "Cursor[N] | N | int") -> N:
        """Resolve a cursor, node, or node id to a node belonging to this graph."""
        self._require_root()
        if isinstance(at, Path):
            at = at.node
        if isinstance(at, AgentNode):
            if self._nodes_by_id.get(at.id) is not at:
                raise ValueError(f"node {at.id} does not belong to this graph")
            return at  # type: ignore[return-value]
        if isinstance(at, int):
            if at not in self._nodes_by_id:
                raise ValueError(f"unknown node id: {at}")
            return self._nodes_by_id[at]
        raise TypeError(f"cannot resolve a {type(at).__name__} to a node")

    def cursor(self, at: "Cursor[N] | N | int") -> Cursor[N]:
        """Resolve to a full cursor. A bare node/id behind a merge has several lineages — an arbitrary
        one is chosen; pass a real cursor when lineage matters (display, branch parentage)."""
        tree = self._require_root()
        if isinstance(at, Path):
            if not tree.is_valid(at):
                raise ValueError("stale cursor: path no longer exists in the graph")
            return at
        return next(tree.paths_to(self.node(at)))

    def children(self, at: "Cursor[N] | N | int") -> tuple[N, ...]:
        """Children of ``at``'s node, in creation order."""
        return self._require_root().children(self.node(at))

    # -------------------------------------------------------------
    # Communication — forwarded to the resolved node
    # -------------------------------------------------------------

    def send(self, at: "Cursor[N] | N | int", payload: AgentPayload, eager: bool = False) -> None:
        self.node(at).send(payload, eager)

    def stream(
        self,
        at: "Cursor[N] | N | int",
        stream_context: AgentStreamingContext,
        payloads: list[AgentPayload] | None = None,
    ) -> None:
        self.node(at).stream(stream_context, payloads)

    async def invoke(self, at: "Cursor[N] | N | int", payloads: list[AgentPayload] | None = None) -> InvokeResult:
        return await self.node(at).invoke(payloads)

    def cancel(self, at: "Cursor[N] | N | int") -> None:
        self.node(at).cancel()

    # -------------------------------------------------------------
    # Topology — branch & merge
    # -------------------------------------------------------------

    async def branch(self, at: "Cursor[N] | N | int") -> Cursor[N]:
        """Branch a new conversation off ``at``'s end state; freezes the parent, returns the child's
        cursor. Branching from an already-frozen node is legal (that is what multiple children are);
        branching a BUSY node is not — its state is mid-flight.

        The child's thread is seeded from the parent's checkpointed state; the ``add_messages`` reducer
        preserves message ids, so the child's prompt prefix stays byte-identical to the parent's (cache
        spine) and merge-by-union stays possible. Node-local channels ride across via ``child.derive``.
        """
        tree = self._require_root()
        path = self.cursor(at)
        parent = path.node
        if parent.busy:
            raise RuntimeError(f"cannot branch node {parent.id} while a run is in flight")

        child = self.make_node()

        values = await parent.agent_state
        if values:
            await child.seed_state(values)

        child.derive(parent)

        tree.add_edge(parent, child)
        parent.frozen = True
        self._publish_topology()
        return Path(child, path)

    async def merge(self, into: "Cursor[N] | N | int", from_: "Cursor[N] | N | int") -> Cursor[N]:
        """Merge two conversations into a fresh child below both parents. EXPERIMENTAL — the app does
        not exercise merges yet; these are v1 baseline semantics, expected to be ironed out:

        - Messages: union by id with ``into`` as the spine. The shared ancestor prefix lands once (in
          ``into``-order) and only ``from_``'s divergent suffix is new — the child continues ``into``'s
          prompt prefix and pays new tokens only for the suffix.
        - Non-message state: ``into``'s only. ``from_``'s workflow state (flashcard/commit proposals,
          review session, ...) is DROPPED — merging half-finished workflows is incoherent until a real
          use case says otherwise.
        - Node-local channels: carried by ``child.derive(into, merged_from=from_)`` — the subclass
          decides how to reconcile the two parents' channels.
        - TODO: a "summary" merge variant — inject a summary of ``from_`` rather than its raw suffix.
        - TODO: prompt-engine treatment of the seam — a blanket suffix append may not be what the model
          should see (the engine may want a merge marker message at the boundary, or a reorder).

        The returned cursor takes the ``into`` lineage as its parent path, so the default display
        lineage and the cache spine coincide.
        """
        tree = self._require_root()
        into_path = self.cursor(into)
        a = into_path.node
        b = self.node(from_)

        if a is b:
            raise ValueError("cannot merge a node with itself")
        if tree.reachable(a, b) or tree.reachable(b, a):
            raise ValueError(f"cannot merge nodes {a.id} and {b.id}: one is an ancestor of the other")
        if a.busy or b.busy:
            raise RuntimeError("cannot merge while either node has a run in flight")

        child = self.make_node()

        # into's full state is the spine; from_'s messages then append as a deduped suffix.
        a_values = await a.agent_state
        if a_values:
            await child.seed_state(a_values)
        b_messages = (await b.agent_state).get("messages") or []
        if b_messages:
            await child.seed_state({"messages": b_messages})

        child.derive(a, merged_from=b)

        tree.add_edge(a, child)
        tree.add_edge(b, child)
        a.frozen = True
        b.frozen = True
        self._publish_topology()
        return Path(child, into_path)
