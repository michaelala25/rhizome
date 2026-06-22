"""Resource-loader VM — the panel's view-model over the ``resources_new`` stores.

What it is
----------
A thin controller in front of the three load-state stores. The minimum-description-length algebra
(load a resource → its sections read loaded, etc.) already lives in the stores; this VM only

- projects each tree node's state across the two axes the UI exposes,
- filters which resources are *visible* (search + topic selection — pure view filters that never
  touch load state), and
- turns user gestures into store writes (``toggle_index`` / ``cycle_context`` / ``toggle_context``),
  taking the target node explicitly (cursor lives view-side).

The two axes
------------
Load state is two *independent* axes, mapped one-to-one onto the stores:

- **index** (bool)            → ``ResourceIndexStore``
- **context** ∈ NONE/LOCAL/GLOBAL → the two ``ResourceContextStore``s; LOCAL and GLOBAL are mutually
  exclusive (one context channel per node at a time), enforced on write.

Index and context are deliberately orthogonal: a node may be both indexed and globally-stuffed
(``[IG-]``). That is redundant — the resource then sits in both the prompt prefix and the index — but
harmless, and the simpler rule is worth the occasional double-representation.

Honest dashes
-------------
The stores are the source of truth and live elsewhere (on the conversation graph and its nodes), so
each is optional here. A missing store makes its axis *unavailable*: the view paints ``-`` for that
slot and the corresponding gesture goes inert. The ``ResourceTree`` — the content-free skeleton the
stores key on — is the one required dependency, because it is what "which resources exist" is read
from. So the loader displays correctly even before any store is wired.

The local store is a swappable reference: it points at the *current conversation leaf's* node-local
context store, and the workspace re-points it (``set_local_context_store``) whenever the graph cursor
moves to a different branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rhizome.app.browser.shared.searchable import SearchableModelMixin
from rhizome.app.model import ViewModelBase
from rhizome.db.operations import (
    fetch_resource_labels,
    fetch_resource_metadata,
    fetch_topic_resource_links,
)
from rhizome.resources_new import ResourceContextStore, ResourceIndexStore, ResourceTree, ResourceTreeNode

from .topic_tree import TopicTreeModel


class ContextScope(Enum):
    """Which context channel a node is stuffed into (if any). LOCAL and GLOBAL are mutually exclusive;
    the loader cycles NONE → LOCAL → GLOBAL → NONE, skipping channels whose store isn't wired."""

    NONE = "none"
    LOCAL = "local"
    GLOBAL = "global"


@dataclass(frozen=True)
class NodeState:
    """One node's load state across the two axes, as the glyphs read it. Axis *availability* (whether
    a store is wired at all) is a panel-wide fact the view reads off the VM separately, not per node."""

    indexed: bool
    context: ContextScope


@dataclass(frozen=True)
class ResourceDisplayNode:
    """A node in the filtered display forest the view walks: the ``ResourceTreeNode`` the stores/glyphs
    key on, its label, and its children. ``estimated_tokens`` / ``section_count`` are resource-only
    (the dim info row); sections leave them at their defaults."""

    node: ResourceTreeNode
    label: str
    children: list["ResourceDisplayNode"] = field(default_factory=list)
    estimated_tokens: int | None = None
    section_count: int = 0

    @property
    def is_resource(self) -> bool:
        return self.node.kind == "resource"

    @property
    def id(self) -> int:
        return self.node.id


@dataclass(frozen=True)
class AxisStats:
    """One axis's footprint: resources touched, sections covered, and an approximate token weight.

    ``tokens`` sums each covered resource's ``estimated_tokens`` whole — a partially loaded resource
    still counts its full weight, since sections carry no token data of their own.
    # TODO: attribute tokens at section granularity once sections expose their own token estimates.
    """

    resources: int
    sections: int
    tokens: int


@dataclass(frozen=True)
class LoadStats:
    """Load picture for the status line: the resource counts (current filter set + library total) plus
    each axis's footprint (resources / sections / tokens, over the whole library)."""

    total_resources: int
    visible_resources: int
    indexed: AxisStats
    context_global: AxisStats
    context_local: AxisStats


class ResourceLoaderModel(SearchableModelMixin):
    """Loader panel VM. See module docstring."""

    class Callbacks(ViewModelBase.Callbacks):
        # Visible forest changed — the view rebuilds its tree (load, search, topic filter, local-store
        # swap). Glyphs for one resource changed — repaint just that subtree; ``None`` means "every
        # visible resource" (a wholesale glyph change with no structural edit).
        OnDataChanged      = "OnDataChanged"
        OnLoadStateChanged = "OnLoadStateChanged"

    def __init__(
        self,
        session_factory: Any,
        tree: ResourceTree,
        *,
        index: ResourceIndexStore | None = None,
        global_context: ResourceContextStore | None = None,
        local_context: ResourceContextStore | None = None,
    ) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnDataChanged:      None,
            self.Callbacks.OnLoadStateChanged: int | None,
        })

        self._session_factory = session_factory
        self._tree = tree
        self._index = index
        self._global = global_context
        self._local = local_context

        # Whole-library display data, fetched by ``load`` (structure comes from ``tree``).
        self._metadata: dict[int, tuple[str, int | None]] = {}   # resource id -> (name, est. tokens)
        self._section_titles: dict[int, str] = {}                # section id -> title
        self._resource_topics: dict[int, frozenset[int]] = {}    # resource id -> linked topic ids

        # View filters — pure visibility, never load state. Empty == no filter.
        self._search: str = ""
        self._selected_topics: frozenset[int] = frozenset()

        # The filtered forest the view renders; rebuilt by ``_rebuild_forest``.
        self._roots: list[ResourceDisplayNode] = []

        # The topic-filter rail: the loader's own child VM (the bottom half of the panel). Its
        # cascading selection feeds straight into the resource filter; this VM drives the rebuild.
        self.topic_filter = TopicTreeModel(session_factory)
        self.topic_filter.subscribe(
            self.topic_filter.Callbacks.OnSelectionChanged, self._on_topic_selection_changed
        )

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def roots(self) -> list[ResourceDisplayNode]:
        """The visible (filtered) resource forest the view walks to build its tree."""
        return self._roots

    @property
    def index_available(self) -> bool:
        return self._index is not None

    @property
    def global_context_available(self) -> bool:
        return self._global is not None

    @property
    def local_context_available(self) -> bool:
        return self._local is not None

    def node_state(self, node: ResourceTreeNode) -> NodeState:
        """The node's state across both axes — what the glyphs paint. Reads the stores' walk-up
        coverage, so a node under a loaded ancestor reads loaded too."""
        indexed = self._index is not None and self._index.is_loaded(node)
        return NodeState(indexed=indexed, context=self._context_scope(node))

    def stats(self) -> LoadStats:
        """Load picture for the status line: resource counts (filter set + library total) plus per-axis
        footprints. The filter-set count is computed on demand so it's correct before any forest build."""
        return LoadStats(
            total_resources=len(self._tree.roots),
            visible_resources=sum(1 for n in self._tree.roots if self._resource_visible(n.id)),
            indexed=self._axis_stats(self._index),
            context_global=self._axis_stats(self._global),
            context_local=self._axis_stats(self._local),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Re-pull everything from the DB: refresh the tree skeleton, prune the stores against it,
        re-fetch display metadata + topic links, and rebuild the visible forest. Re-callable after
        out-of-band resource/topic CRUD. Emits ``OnDataChanged``."""
        await self._tree.refresh()
        for store in (self._index, self._global, self._local):
            if store is not None:
                store.prune()

        resource_ids = [n.id for n in self._tree.roots]
        section_ids = [c.id for n in self._tree.roots for c in self._descendants(n)]
        async with self._session_factory() as session:
            self._metadata = await fetch_resource_metadata(session)
            _, self._section_titles = await fetch_resource_labels(session, [], section_ids)
            links = await fetch_topic_resource_links(session)

        topics: dict[int, set[int]] = {}
        for topic_id, rid in links:
            topics.setdefault(rid, set()).add(topic_id)
        self._resource_topics = {rid: frozenset(ts) for rid, ts in topics.items()}

        self._rebuild_forest()
        await self.topic_filter.load()

    def _on_topic_selection_changed(self) -> None:
        """The topic rail's selection changed — refresh the visible resources against it."""
        self.set_topic_filter(self.topic_filter.selected_ids)

    # ------------------------------------------------------------------
    # View filters (visibility only)
    # ------------------------------------------------------------------

    def set_search(self, query: str) -> None:
        """Narrow the visible resources to those whose name matches ``query`` (case-insensitive
        substring). Empty clears the filter. View-only — load state is untouched. Equality-guarded."""
        query = query.strip()
        if query == self._search:
            return
        self._search = query
        self._rebuild_forest()

    def set_topic_filter(self, topic_ids: frozenset[int] | set[int] | None) -> None:
        """Narrow the visible resources to those linked to any of ``topic_ids``. Empty/``None`` clears
        the filter (show everything). The topic tree's cascading selection set is passed straight in.
        View-only; equality-guarded."""
        selected = frozenset(topic_ids or ())
        if selected == self._selected_topics:
            return
        self._selected_topics = selected
        self._rebuild_forest()

    # ------------------------------------------------------------------
    # Load-state mutators (the target node is passed in — cursor lives view-side)
    # ------------------------------------------------------------------

    def toggle_index(self, node: ResourceTreeNode) -> None:
        """Flip the node's index axis (its whole subtree). Inert when no index store is wired."""
        if self._index is None:
            return
        if self._index.set_loaded(node, not self._index.is_loaded(node)):
            self._emit_load_changed(node)

    def cycle_context(self, node: ResourceTreeNode) -> None:
        """Advance the node's context channel NONE → LOCAL → GLOBAL → NONE, skipping channels whose
        store isn't wired. Inert when neither context store is wired."""
        scopes = self._available_scopes()
        if scopes == [ContextScope.NONE]:
            return
        current = self._context_scope(node)
        nxt = scopes[(scopes.index(current) + 1) % len(scopes)] if current in scopes else ContextScope.NONE
        if self._set_context(node, nxt):
            self._emit_load_changed(node)

    def toggle_context(self, node: ResourceTreeNode, scope: ContextScope) -> None:
        """Toggle one context channel on the node: clear it if already on ``scope``, else switch to
        ``scope`` (the other channel is cleared — one context channel per node at a time). Inert when
        that channel's store isn't wired. ``scope`` is LOCAL or GLOBAL; NONE is a no-op."""
        store = (
            self._global if scope is ContextScope.GLOBAL
            else self._local if scope is ContextScope.LOCAL else None
        )
        if store is None:
            return
        target = ContextScope.NONE if self._context_scope(node) is scope else scope
        if self._set_context(node, target):
            self._emit_load_changed(node)

    # ------------------------------------------------------------------
    # Store wiring (workspace responsibility)
    # ------------------------------------------------------------------

    def set_local_context_store(self, store: ResourceContextStore | None) -> None:
        """Re-point the local context channel at the current conversation leaf's node-local store. The
        workspace calls this on every graph-cursor move. Repaints all glyphs (the orange column's
        meaning changed); no-op when the store is unchanged."""
        if store is self._local:
            return
        self._local = store
        self._rebuild_forest()

    # ------------------------------------------------------------------
    # Internals — context axis
    # ------------------------------------------------------------------

    def _context_scope(self, node: ResourceTreeNode) -> ContextScope:
        # Mutual exclusion is enforced on write, so at most one channel holds the node; GLOBAL wins a
        # read if both ever do.
        if self._global is not None and self._global.is_loaded(node):
            return ContextScope.GLOBAL
        if self._local is not None and self._local.is_loaded(node):
            return ContextScope.LOCAL
        return ContextScope.NONE

    def _available_scopes(self) -> list[ContextScope]:
        scopes = [ContextScope.NONE]
        if self._local is not None:
            scopes.append(ContextScope.LOCAL)
        if self._global is not None:
            scopes.append(ContextScope.GLOBAL)
        return scopes

    def _set_context(self, node: ResourceTreeNode, scope: ContextScope) -> bool:
        # Write both channels so the mutual-exclusion invariant holds regardless of prior state;
        # report whether either channel actually moved (so callers can skip a no-op repaint).
        changed = False
        if self._global is not None:
            changed |= self._global.set_loaded(node, scope is ContextScope.GLOBAL)
        if self._local is not None:
            changed |= self._local.set_loaded(node, scope is ContextScope.LOCAL)
        return changed

    # ------------------------------------------------------------------
    # Internals — forest + filtering
    # ------------------------------------------------------------------

    def _rebuild_forest(self) -> None:
        self._roots = [
            self._build_resource(node) for node in self._tree.roots if self._resource_visible(node.id)
        ]
        self.emit(self.Callbacks.OnDataChanged)

    def _resource_visible(self, resource_id: int) -> bool:
        if self._search:
            name, _ = self._metadata.get(resource_id, ("", None))
            if self._search.lower() not in name.lower():
                return False
        if self._selected_topics:
            if not (self._resource_topics.get(resource_id, frozenset()) & self._selected_topics):
                return False
        return True

    def _build_resource(self, node: ResourceTreeNode) -> ResourceDisplayNode:
        name, tokens = self._metadata.get(node.id, (f"Resource {node.id}", None))
        children = [self._build_section(c) for c in self._tree.children(node)]
        section_count = sum(1 for _ in self._descendants(node))
        return ResourceDisplayNode(node, name, children, estimated_tokens=tokens, section_count=section_count)

    def _build_section(self, node: ResourceTreeNode) -> ResourceDisplayNode:
        label = self._section_titles.get(node.id, f"Section {node.id}")
        children = [self._build_section(c) for c in self._tree.children(node)]
        return ResourceDisplayNode(node, label, children)

    def _descendants(self, node: ResourceTreeNode) -> list[ResourceTreeNode]:
        """Every section node under ``node`` (depth-first), excluding ``node`` itself."""
        out: list[ResourceTreeNode] = []
        stack = list(self._tree.children(node))
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(self._tree.children(current))
        return out

    def _axis_stats(self, store: ResourceContextStore | ResourceIndexStore | None) -> AxisStats:
        """One store's footprint. A resource counts if it or any descendant is covered (a section-only
        load still tallies its owning resource and its full token weight); sections count one by one."""
        if store is None:
            return AxisStats(0, 0, 0)
        resources = sections = tokens = 0
        for root in self._tree.roots:
            covered_sections = sum(1 for d in self._descendants(root) if store.is_loaded(d))
            sections += covered_sections
            if store.is_loaded(root) or covered_sections:
                resources += 1
                tokens += self._tokens(root.id)
        return AxisStats(resources=resources, sections=sections, tokens=tokens)

    def _tokens(self, resource_id: int) -> int:
        _, tokens = self._metadata.get(resource_id, ("", None))
        return tokens or 0

    def _emit_load_changed(self, node: ResourceTreeNode) -> None:
        # Glyphs repaint per owning resource; map a section back up to its resource root.
        current = node
        while current.kind != "resource":
            parent = self._tree.parent(current)
            if parent is None:
                break
            current = parent
        self.emit(self.Callbacks.OnLoadStateChanged, current.id)
