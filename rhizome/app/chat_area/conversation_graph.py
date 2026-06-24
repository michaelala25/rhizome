"""Conversation graph: the app-side layer over the agent graph.

``ConversationNode`` extends ``AgentNode`` with the per-branch state the app cares about and the agent
layer does not: the node-local resource store, a ``feed`` of display items, a branch ``name``, the
``pending_interrupt`` handle, and ``last_visited_child`` navigation memory. ``ConversationGraph`` extends
``AgentGraph`` with ownership of the graph-global resource stores (shared context + index), the feed API
(append/remove/clear plus path-level queries), renames, and cursor navigation helpers (``descend`` /
``ascend`` / ``swap_sibling``) that restore previously-visited paths through the graph.

Topology semantics are inherited unchanged: ``branch`` and ``merge`` mean exactly what they mean on
``AgentGraph`` (one child per branch, parents freeze). Conversation-level policies — e.g. creating a
continuation sibling on the first user-initiated branch — belong to the layer above, composed out of
these primitives.

Feed entries are opaque to the graph (the layer above decides what a feed item *is*); the graph wraps
each entry in a ``ConversationItem`` carrying an id minted from one graph-wide counter. The id — not
the feed position — is an item's identity: positions shift as out-of-band mutations land, ids never do.

The graph is a plain model, not a view-model. It emits model-level events through ``CallbackHost``
groups (feed mutations, renames) for whoever composes it — typically a chat-area VM translating them
into view-facing callbacks, with room for other subscribers (e.g. a graph visualizer) later.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable

from rhizome.agent.app_context import AppContextHookService, LocalAppContextStore
from rhizome.agent.engine import UsageReport
from rhizome.agent.engine_events import EngineEventsChannel
from rhizome.agent.graph import AgentGraph, AgentNode, Cursor, WorkerScheduler
from rhizome.agent.runtime import AgentRuntime
from rhizome.agent.session import AgentSession
from rhizome.resources import ResourceContextStore, ResourceIndexStore
from rhizome.utils.data_structures import Path


@dataclass(frozen=True)
class ConversationItem[E]:
    """A feed entry wrapped with a stable, monotonically-assigned id.

    The id is the canonical handle for feed mutations (remove, future replace/ping). Position is not
    identity: an item's index can shift as out-of-band operations land, so consumers that need to
    address a specific item must hold its id rather than its index.
    """

    id: int
    entry: E


class ConversationNode[E](AgentNode):
    """An ``AgentNode`` carrying the app-facing state of one conversation branch, plus the node-local
    resource store, app-settings store, and engine-events channel.

    The agent-layer fields (session, worker) arrive through the base constructor; ``resources``,
    ``app_state``, ``engine_events``, and the conversation bookkeeping are added here. All three are
    read-only handles on purpose: the SAME objects are wired into the session's frozen compile context, so
    they must never be swapped. The two stores are seeded from the parent on a branch (``copy_from``); the
    engine-events channel is not — each node gets a fresh one carrying its own id (a stateless bus has
    nothing to copy).
    """

    def __init__(
        self,
        session: AgentSession,
        resources: ResourceContextStore | None,
        app_state: LocalAppContextStore | None,
        engine_events: EngineEventsChannel | None,
        scheduler: WorkerScheduler,
    ) -> None:
        super().__init__(session, scheduler)

        self._resources = resources
        # Node-local context store, the SAME object wired into the session's compile context. Read-only
        # (see ``resources``): identity is fixed for the node's life; only its content is copied across.

        self._app_state = app_state
        # Node-local app-settings store (mode), the SAME object wired into the session's compile context.
        # Read-only handle for the same reason as ``resources``; content copied across on branch.

        self._engine_events = engine_events
        # Per-node engine→app event channel, the SAME object wired into the session's compile context.
        # Read-only handle like the stores, but never seeded on branch: a stateless bus has nothing to
        # copy, and each node's channel carries its own id.

        self.feed: list[ConversationItem[E]] = []
        # Display items for this branch segment. The visible conversation at a cursor is the
        # concatenation of feeds along the cursor path (see ConversationGraph.visible_feed).

        self.name: str | None = None
        # Optional human label, mutated via ConversationGraph.rename so subscribers hear about it.

        self.pending_interrupt: E | None = None
        # The interrupt entry awaiting user input on this branch, if any. Also present in the feed;
        # this slot is the "is this branch blocked on the user?" flag consumers derive input state
        # from. Owned by the layer that presents/resolves interrupts — plain attribute, no events.

        self.last_visited_child: "ConversationNode[E] | None" = None
        # Most recently traversed child of this node, recorded on every navigation through it. Lets
        # descend/swap_sibling restore the previously checked-out path below a revisited branch
        # point instead of stopping at the immediate child. Node-level on purpose: under merges,
        # every lineage through a node shares the same memory.

        self.usage_report: UsageReport | None = None
        # Latest token-usage report for this branch — display state, like ``feed``. The stream router
        # refreshes it as the conversation runs; the status bar reads it on cursor change. Inherited on
        # branch (``derive``): the child's seeded state matches the parent's, so the parent's report is
        # accurate until the child diverges.

    @property
    def resources(self) -> ResourceContextStore | None:
        """The node-local context store (never reassigned — see the class docstring)."""
        return self._resources

    @property
    def app_state(self) -> LocalAppContextStore | None:
        """The node-local app-settings store (never reassigned — see the class docstring)."""
        return self._app_state

    @property
    def engine_events(self) -> EngineEventsChannel | None:
        """The per-node engine→app event channel (never reassigned — see the class docstring)."""
        return self._engine_events

    def derive(
        self, parent: "ConversationNode[E]", merged_from: "ConversationNode[E] | None" = None
    ) -> None:
        """Carry the node-local stores' state across a branch/merge edge.

        Copies CONTENT into this child's stores (their identities are wired into the session's frozen
        compile context, so they must not be swapped for copies of the parent's). On a branch the child
        inherits the parent's local load-state (and its ``local-resource-ctx-{rid}`` messages) and its
        active mode, then diverges; on a merge only the spine ``parent``'s resource set is taken for now —
        ``merged_from``'s loaded-set is dropped.

        TODO(merge): union both parents' loaded-sets, re-normalized and re-reconciled against the global
        store; deferred until merge is exercised for real. The engine keys local context messages
        per-resource (``local-resource-ctx-{rid}``), so distinct resources from the two parents survive a
        union untouched; the only clobber is two parents loading the *same* resource with different
        sections (same id, replace-in-place), which is exactly the reconciliation this TODO defers.
        """
        if self._resources is not None and parent.resources is not None:
            self._resources.copy_from(parent.resources)
        if self._app_state is not None and parent.app_state is not None:
            self._app_state.copy_from(parent.app_state)

        # The child's seeded checkpoint matches the parent's, so the parent's usage report describes it
        # accurately until it diverges — carry it so a freshly-branched node shows usage before its first run.
        self.usage_report = parent.usage_report


class ConversationGraph[E](AgentGraph[ConversationNode[E]]):
    """An ``AgentGraph`` whose nodes carry conversation state, plus the APIs that manage it.

    Navigation helpers take and return cursors (``Path`` values) but own no "current" cursor — the
    checkout position belongs to whoever drives the graph. What the graph *does* own is the
    per-node ``last_visited_child`` memory those helpers read and record.
    """

    class Callbacks(AgentGraph.Callbacks):
        OnFeedAppended = "OnFeedAppended"
        OnFeedRemoved  = "OnFeedRemoved"
        OnFeedCleared  = "OnFeedCleared"
        OnNodeRenamed  = "OnNodeRenamed"

    def __init__(
        self,
        runtime: AgentRuntime,
        scheduler: WorkerScheduler = asyncio.create_task,
        *,
        agent_key: str = "root",
        resource_context: ResourceContextStore | None = None,
        resource_index: ResourceIndexStore | None = None,
        app_context_hooks: AppContextHookService | None = None,
        local_resources_factory: Callable[[], ResourceContextStore] | None = None,
    ) -> None:
        self.resource_context = resource_context
        # Graph-global context store, shared by every node's compile context.
        self.resource_index = resource_index
        # Graph-global index store, one instance for the whole graph.
        self.app_context_hooks = app_context_hooks
        # The workspace-scoped app-context hook service, shared by every node's compile context; the engine
        # renders its facts as ephemeral tail context. None disables the channel (the engine renders nothing).
        self._local_resources_factory = local_resources_factory
        # Produces a fresh node-local context store per node; None disables local stores.

        self._next_item_id = 0
        super().__init__(runtime, scheduler, agent_key=agent_key)

        # Conversation-level groups, on top of the base graph's. Registered after super().__init__
        # (which initializes the CallbackHost registry); nothing during construction emits them.
        self.make_callback_groups({
            self.Callbacks.OnFeedAppended: (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedRemoved:  (ConversationNode, ConversationItem),
            self.Callbacks.OnFeedCleared:  ConversationNode,
            self.Callbacks.OnNodeRenamed:  ConversationNode,
        })

    @property
    def resources(self) -> tuple[ResourceContextStore | None, ResourceIndexStore | None]:
        """The graph-global (context store, index store) pair."""
        return self.resource_context, self.resource_index

    def _make_node(self, node_id: int) -> ConversationNode[E]:
        """Build a node carrying fresh node-local stores (resources + app settings) and a fresh per-node
        engine-events channel, wired (alongside the graph-global context/index stores and app-context
        hooks, the shared topology view, and this node's id) into the session's compile context."""
        local = self._local_resources_factory() if self._local_resources_factory else None
        app_state = LocalAppContextStore()
        engine_events = EngineEventsChannel(node_id)
        session = self._runtime.new(
            self._agent_key,
            local_resources=local,
            global_resources=self.resource_context,
            resource_index=self.resource_index,
            app_context_hooks=self.app_context_hooks,
            topology=self._topology,
            node_id=node_id,
            app_state=app_state,
            engine_events=engine_events,
        )
        return ConversationNode(session, local, app_state, engine_events, self._scheduler)

    # -------------------------------------------------------------
    # Feed
    # -------------------------------------------------------------

    def append(self, at: "Cursor | ConversationNode[E] | int", entry: E) -> ConversationItem[E]:
        """Append ``entry`` to ``at``'s feed under a fresh id; emits ``OnFeedAppended``.

        Frozen nodes refuse appends — their feeds are sealed history. (In-flight turns are safe:
        a node cannot freeze while busy, because ``branch``/``merge`` reject busy parents.)
        """
        node = self.node(at)
        if node.frozen:
            raise ValueError(f"node {node.id} is frozen; its feed is sealed history")

        item = ConversationItem(self._next_item_id, entry)
        self._next_item_id += 1
        node.feed.append(item)
        self.emit(self.Callbacks.OnFeedAppended, node, item)
        return item

    def remove(self, at: "Cursor | ConversationNode[E] | int", item_id: int) -> ConversationItem[E] | None:
        """Remove the item with ``item_id`` from ``at``'s feed; returns it, or ``None`` if absent.

        Allowed on frozen nodes: removal is cleanup (a stale indicator, a transient widget), not
        new history. Emits ``OnFeedRemoved`` when something was actually removed.
        """
        node = self.node(at)
        for i, item in enumerate(node.feed):
            if item.id == item_id:
                del node.feed[i]
                self.emit(self.Callbacks.OnFeedRemoved, node, item)
                return item
        return None

    def clear_feed(self, at: "Cursor | ConversationNode[E] | int") -> None:
        """Drop every item in ``at``'s feed; emits ``OnFeedCleared``. No-op on an already-empty
        feed. Frozen nodes refuse — same sealed-history rule as ``append``."""
        node = self.node(at)
        if node.frozen:
            raise ValueError(f"node {node.id} is frozen; its feed is sealed history")
        if not node.feed:
            return
        node.feed.clear()
        self.emit(self.Callbacks.OnFeedCleared, node)

    def visible_feed(self, cursor: "Cursor | ConversationNode[E] | int") -> list[ConversationItem[E]]:
        """Concatenated feeds of every node on the cursor path, in path order — what a view renders
        for that checkout."""
        out: list[ConversationItem[E]] = []
        for node in self.cursor(cursor):
            out.extend(node.feed)
        return out

    def feed_segments(
        self, cursor: "Cursor | ConversationNode[E] | int"
    ) -> list[tuple[ConversationNode[E], list[ConversationItem[E]]]]:
        """Per-node ``(node, feed-snapshot)`` pairs along the cursor path, root-to-leaf.

        Snapshots are shallow copies so callers cannot mutate node-owned feeds by accident. Use this
        when the consumer needs to know which node each segment belongs to — e.g. per-depth display
        containers, or inserting boundary indicators between segments.
        """
        return [(node, list(node.feed)) for node in self.cursor(cursor)]

    # -------------------------------------------------------------
    # Names
    # -------------------------------------------------------------

    def rename(self, at: "Cursor | ConversationNode[E] | int", name: str | None) -> None:
        """Set a branch's display name (``None`` clears it); emits ``OnNodeRenamed``.
        Equality-guarded: assigning the current name emits nothing."""
        node = self.node(at)
        if node.name == name:
            return
        node.name = name
        self.emit(self.Callbacks.OnNodeRenamed, node)

    # -------------------------------------------------------------
    # Navigation
    # -------------------------------------------------------------
    #
    # Helpers return new cursors and never store one; each records the path it returns into the
    # per-node last-visited memory, so the next descent through any node on it restores this
    # checkout. ``ascend`` deliberately skips re-deepening — truncating upward is an explicit
    # request, and the memory below the new leaf stays warm for the next descend/swap.

    def record_visit(self, cursor: Cursor) -> None:
        """Stamp ``cursor`` into the last-visited memory of every node along it."""
        nodes = self.cursor(cursor).nodes()
        for parent, child in zip(nodes, nodes[1:]):
            parent.last_visited_child = child

    def deepen(self, cursor: Cursor) -> Cursor:
        """Extend ``cursor`` leafward by chasing last-visited memory until it runs out.

        Stops at a node with no recorded child or whose recorded child is no longer one of its
        children (defensive — the topology is append-only today, but the memory must not be able to
        corrupt a cursor if that ever changes).
        """
        path = self.cursor(cursor)
        while True:
            recorded = path.node.last_visited_child
            if recorded is None or recorded not in self._tree.children(path.node):
                return path
            path = Path(recorded, path)

    def descend(self, cursor: Cursor, child: "ConversationNode[E] | int") -> Cursor:
        """Step from the cursor's leaf into ``child``, then deepen through last-visited memory —
        re-entering a previously-visited subtree lands on the deepest previously-seen node, not the
        immediate child. Raises ``KeyError`` if ``child`` is not a child of the leaf."""
        path = self._tree.extend(self.cursor(cursor), self.node(child))
        path = self.deepen(path)
        self.record_visit(path)
        return path

    def ascend(self, cursor: Cursor, *, to: "ConversationNode[E] | int | None" = None) -> Cursor:
        """Truncate the cursor: one level by default, or so that ``to`` becomes the new leaf.

        ``to`` must be a proper ancestor on the cursor path — this is "un-descend out of *this*
        branch point" for callers holding a node several levels above the leaf. Raises
        ``ValueError`` at the root (default form) or when ``to`` is not a proper ancestor.
        """
        path = self.cursor(cursor)
        if to is None:
            if path.parent is None:
                raise ValueError("cannot ascend: cursor is at the root")
            result = path.parent
        else:
            target = self.node(to)
            step = path.parent
            while step is not None and step.node is not target:
                step = step.parent
            if step is None:
                raise ValueError(f"node {target.id} is not a proper ancestor on this cursor")
            result = step

        self.record_visit(result)
        return result

    def swap_sibling(
        self,
        cursor: Cursor,
        direction: int,
        *,
        at: "ConversationNode[E] | int | None" = None,
    ) -> Cursor:
        """Move horizontally to a sibling at a branch point on the cursor path, then deepen.

        ``direction`` is ``-1`` (left) / ``+1`` (right) across the branch point's children in
        creation order. ``at`` names the branch-point node whose descended child swaps; the default
        is the leaf's parent. The swapped-to sibling deepens through its own last-visited memory, so
        swapping away and back restores the previously checked-out path below it. Raises
        ``ValueError`` when ``at`` isn't a proper ancestor or no sibling exists in that direction.
        """
        if direction not in (-1, 1):
            raise ValueError("direction must be -1 or +1")
        path = self.cursor(cursor)

        # Locate the branch-point step and the step of its currently-descended child.
        child_step, parent_step = path, path.parent
        if at is not None:
            target = self.node(at)
            while parent_step is not None and parent_step.node is not target:
                child_step, parent_step = parent_step, parent_step.parent
        if parent_step is None:
            where = "the root has no siblings" if at is None else "node is not a proper ancestor on this cursor"
            raise ValueError(f"cannot swap sibling: {where}")

        siblings = self._tree.children(parent_step.node)
        idx = siblings.index(child_step.node) + direction
        if not 0 <= idx < len(siblings):
            raise ValueError("no sibling in that direction")

        result = self.deepen(Path(siblings[idx], parent_step))
        self.record_visit(result)
        return result
