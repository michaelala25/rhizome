"""Load-state arithmetic over the ``ResourceTree``, and the store objects that hold it.

A *description* is a set of tree nodes meaning "these subtrees are loaded". Stores keep descriptions
in **canonical minimal form**: no entry's subtree covers another's, and a node whose children are all
loaded is promoted to stand in for them ("all children loaded => parent loaded", cascading upward).
Canonical form is unique for a given coverage, so plain set equality is a sound "nothing changed"
fast path, and entry-level set difference is a well-defined delta.

The algorithms are deliberately free functions over ``(sets, tree)`` — stores are dumb containers.
Two pieces of logic intentionally live ELSEWHERE:

- Cross-store policy (keeping global/local context loads disjoint per node) belongs to whoever
  writes to the stores — the resource viewer / app layer — not to the stores themselves.
- Consumption bookkeeping lives with the consumer. Both context channels and the index *message* have
  N consumers (every agent node diffs the desired set against its own per-branch snapshot in
  ``RootAgentState``), so neither store carries that state. The index's *ingestion* has exactly one
  consumer — the single vector index — so ``ResourceIndexStore`` keeps that watermark internally.
"""

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from .index import ResourceVectorStore
from .tree import ResourceTree, ResourceTreeNode


# ========================================================================================================================
# DESCRIPTION ARITHMETIC
# ========================================================================================================================


def expand(description: Iterable[ResourceTreeNode], tree: ResourceTree) -> set[ResourceTreeNode]:
    """Full coverage of a description: every listed node plus all of its descendants.

    Ids no longer in the tree (deleted resources still referenced by an old consumed snapshot) are
    kept as isolated leaves so downstream diffs can still surface their removal.
    """
    covered: set[ResourceTreeNode] = set()
    stack = list(description)
    while stack:
        node = stack.pop()
        if node in covered:
            continue
        covered.add(node)
        stack.extend(tree.children(node))
    return covered


def close_upward(coverage: set[ResourceTreeNode], tree: ResourceTree) -> set[ResourceTreeNode]:
    """Upward closure of the promotion rule: wherever ALL of a node's children are present, the node
    joins the set too, cascading toward the roots."""
    out = set(coverage)

    def visit(node: ResourceTreeNode) -> None:
        children = tree.children(node)
        for child in children:
            visit(child)
        if children and all(child in out for child in children):
            out.add(node)

    for root in tree.roots:
        visit(root)
    return out


def aggregate(coverage: set[ResourceTreeNode], tree: ResourceTree) -> set[ResourceTreeNode]:
    """Minimal description of a coverage set: the antichain of maximal covered nodes.

    Strict subtree rule — a node is covered iff it is in the set AND all its children are covered —
    so aggregating never claims more than the input covers. Promotion is ``close_upward``'s job, not
    this function's. Nodes unreachable from the tree's roots (stale ids) pass through untouched.
    """
    result: set[ResourceTreeNode] = set()
    covered: dict[ResourceTreeNode, bool] = {}
    reachable: set[ResourceTreeNode] = set()

    def visit(node: ResourceTreeNode) -> bool:
        reachable.add(node)
        children = tree.children(node)
        if not children:
            is_covered = node in coverage
        else:
            child_covered = [visit(child) for child in children]  # materialized: no short-circuit
            is_covered = node in coverage and all(child_covered)
            if not is_covered:
                for child in children:
                    if covered[child]:
                        result.add(child)
        covered[node] = is_covered
        return is_covered

    for root in tree.roots:
        if visit(root):
            result.add(root)
    result.update(node for node in coverage if node not in reachable)
    return result


def normalize(description: Iterable[ResourceTreeNode], tree: ResourceTree) -> set[ResourceTreeNode]:
    """Canonical minimal form of an arbitrary description: expand to full coverage, promote
    fully-covered parents, aggregate back down to the antichain."""
    return aggregate(close_upward(expand(description, tree), tree), tree)


@dataclass
class ResourceLoadDelta:
    additions: list[ResourceTreeNode] = field(default_factory=list)
    removals: list[ResourceTreeNode] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.additions or self.removals)


def load_delta(
    before: Iterable[ResourceTreeNode],
    after: Iterable[ResourceTreeNode],
) -> ResourceLoadDelta:
    """Entry-level delta between two canonical descriptions: which entries left, which arrived.

    Deliberately a plain set difference. Canonical form makes entry identity well-defined, so a
    consumer keying content blocks by description entry applies the delta as "drop these blocks,
    fetch and add those". When a promotion rewrites entries (children collapse into their parent)
    the delta says exactly that — remove the child entries, add the parent — at the cost of
    refetching content the parent already covered; correctness stays trivial in exchange.
    """
    before_set, after_set = set(before), set(after)
    return ResourceLoadDelta(
        additions=list(after_set - before_set),
        removals=list(before_set - after_set),
    )


# ========================================================================================================================
# STORES
# ========================================================================================================================


class ResourceStore:
    """Desired load state for one channel, held in canonical minimal form over a shared tree."""

    def __init__(self, tree: ResourceTree, loaded: Iterable[ResourceTreeNode] = ()) -> None:
        self._tree = tree
        self._loaded: set[ResourceTreeNode] = normalize(loaded, tree)

    @property
    def tree(self) -> ResourceTree:
        return self._tree

    @property
    def loaded(self) -> frozenset[ResourceTreeNode]:
        """The canonical minimal description of what this channel has loaded."""
        return frozenset(self._loaded)

    def is_loaded(self, node: ResourceTreeNode) -> bool:
        """Walk-up check against the description. Promotion is already materialized by
        ``normalize``, so an all-children-loaded parent appears in the description itself."""
        current: ResourceTreeNode | None = node
        while current is not None:
            if current in self._loaded:
                return True
            current = self._tree.parent(current)
        return False

    def set_loaded(self, node: ResourceTreeNode, loaded: bool) -> bool:
        """Load or unload a node's subtree; returns whether the description changed.

        Unloading clears the node's full subtree even when the node itself only reads as partially
        loaded (some descendants loaded, the node not covered) — matching how a tri-state checkbox
        behaves when unticked.
        """
        if loaded:
            if self.is_loaded(node):
                return False
            new = normalize([*self._loaded, node], self._tree)
        else:
            coverage = expand(self._loaded, self._tree) - expand([node], self._tree)
            new = aggregate(close_upward(coverage, self._tree), self._tree)

        changed = new != self._loaded
        self._loaded = new
        return changed

    def prune(self) -> bool:
        """Drop entries whose ids no longer exist; call after ``tree.refresh()``. Returns whether
        anything changed."""
        known = {node for node in self._loaded if node in self._tree}
        new = normalize(known, self._tree)
        changed = new != self._loaded
        self._loaded = new
        return changed


class ResourceContextStore(ResourceStore):
    """Context-stuffing channel: one global instance on the ``AgentGraph``, one local instance per
    ``AgentNode``. Carries no consumption state — each consuming node diffs ``loaded`` against its
    own consumed snapshot in ``RootAgentState.consumed_resource_context`` via ``load_delta``.

    Optionally memoizes built content blocks (``cache=True``). Worth it only for the shared global store,
    where one instance backs every branch, so a freshly loaded resource's DB read is paid once across all
    of them; local stores are single-use and leave it off. Entries are keyed by resource id and cleared
    wholesale on any load change — they are valid only for the current desired state, which a change
    invalidates. (Edits to a resource's *text* don't pass through here, so they aren't yet observed; a
    ``tree.refresh()`` + ``prune()`` after resource CRUD is the current backstop.)
    """

    def __init__(
        self, tree: ResourceTree, loaded: Iterable[ResourceTreeNode] = (), *, cache: bool = False
    ) -> None:
        super().__init__(tree, loaded)
        self._content_cache: dict[int, str] | None = {} if cache else None

    async def block(self, resource_id: int, build: Callable[[], Awaitable[str | None]]) -> str | None:
        """The content block for a resource: the cached build, or ``await build()`` (cached on a hit) on a
        miss. With caching off (the default) ``build`` runs every call. Best-effort — concurrent misses for
        one id may both build, which is harmless (identical content)."""
        if self._content_cache is None:
            return await build()
        if resource_id not in self._content_cache:
            built = await build()
            if built is not None:
                self._content_cache[resource_id] = built
            return built
        return self._content_cache[resource_id]

    def set_loaded(self, node: ResourceTreeNode, loaded: bool) -> bool:
        changed = super().set_loaded(node, loaded)
        if changed:
            self._invalidate_cache()
        return changed

    def prune(self) -> bool:
        changed = super().prune()
        if changed:
            self._invalidate_cache()
        return changed

    def copy_from(self, other: "ResourceContextStore") -> None:
        """Adopt ``other``'s description by content. Branch support: this store's identity is wired
        into a session's compile context and must never be swapped for a copy of the parent's."""
        if other._tree is not self._tree:
            raise ValueError("cannot copy between stores built on different trees")
        self._loaded = set(other._loaded)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        if self._content_cache is not None:
            self._content_cache.clear()


class ResourceIndexStore(ResourceStore):
    """Index channel: ONE instance per graph, shared by every branch.

    Two consumption stories, with different scopes:

    - The "what can be queried" reminder is a per-thread message: the prompt engine diffs ``loaded``
      against each branch's snapshot in ``RootAgentState.consumed_resource_index`` and refreshes a
      stable-id message only on a change (mirroring the context channel). The store holds none of
      that — it is per-branch state.
    - ``consume()`` is the single consumer that actually populates the vector index, run lazily from
      compile rather than when the user ticks "load". Its watermark is graph-global and lives here,
      precisely because there is exactly one index to ingest into.
    """

    def __init__(
        self,
        tree: ResourceTree,
        loaded: Iterable[ResourceTreeNode] = (),
        index: ResourceVectorStore | None = None,
    ) -> None:
        super().__init__(tree, loaded)
        self.index = index
        self._consumed: set[ResourceTreeNode] = set()

    @property
    def consumed(self) -> frozenset[ResourceTreeNode]:
        """What the vector index has ingested — the watermark ``consume`` advances. Diverges from
        ``loaded`` only between a load change and the next ``consume``."""
        return frozenset(self._consumed)

    async def consume(self) -> None:
        """Bring the vector index up to the current desired load state.

        Called lazily from the prompt engine's compile, so the index populates on first use rather than
        when the user ticks "load" in the viewer. The internal watermark diffs the desired set against
        what the index last ingested, so a steady-state call does no work; on any change the flat index
        is rebuilt wholesale from the full desired set (``ResourceVectorStore.sync`` owns the DB fetch).

        Without an attached index the watermark still advances — there is nothing to reconcile against,
        so reconciliation is trivially complete. Once the index can report its own contents the watermark
        could become a query against it rather than this private set; if a wholesale rebuild ever stalls a
        compile, hand the whole ``sync`` off to a ``WorkerSchedulerService`` (a coroutine background
        worker) rather than awaiting it inline — the ``asyncio.to_thread`` inside ``sync`` only keeps the
        event loop responsive during the rebuild, it doesn't move the rebuild off the compile path.
        """
        delta = load_delta(self._consumed, self._loaded)
        if not delta:
            return
        if self.index is not None:
            await self.index.sync(self._loaded)
        self._consumed = set(self._loaded)
