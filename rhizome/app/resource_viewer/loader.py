"""``ResourceLoaderModel`` — load/context-stuff state for the current topic's resources.

Terminology
-----------
The loader presents the current topic's resources as a tree: each resource is a root, and its
sections (and subsections) are descendants. Every node can be made available to the agent at one
of two load types — ``INDEX`` (embedded into the vector store, retrieved on demand via the
``query_resources`` tool) or ``CONTEXT`` (stuffed verbatim into the agent's context window) — or
left unloaded. Loading a node always applies to its entire subtree.

Representation
--------------
``_load_state`` maps nodes to load types: an entry at node *X* means "*X* and all of its descendants
are at this type, unless a descendant carries its own overriding entry." Absence of any covering
entry means unloaded. A node's *effective* type is the type of its nearest ancestor-or-self entry
(see :meth:`_effective_type`).

The state is kept **minimal** — the fewest entries that describe the loaded picture (minimum
description length) — and every mutation restores that, so the manager sync reads ``_load_state``
directly. Minimality has two structural consequences: a loaded node never carries descendant entries,
and a node's children never all carry entries of the same type. The second is the rule the interface
relies on — loading every child of a node is the same as loading the node itself.

The load-state algorithm
------------------------
``_apply(key, type | None)`` sets a node's effective type (``None`` = unload), restoring minimality
in four steps:

  1. **Expand** — if the node's effective type comes from a *proper ancestor*, push that ancestor's
     entry down the path to the node, materializing sibling entries so we can change just this node.
  2. **Clear** — drop any entries beneath the node (about to be dominated by what we set).
  3. **Set** — write the node's entry, or remove it for an unload.
  4. **Collapse** — walk up: whenever all of a parent's children carry entries of one type, replace
     them with a single entry on the parent; repeat.

The adjacency walks these steps need (parent / child / descendant / ancestor) come from
:class:`_ResourceTreeIndex`, rebuilt from the resource list on every fetch.

Cursor & embedding
------------------
The cursor lives in the view; this VM mirrors only the highlighted *node* (pushed via
:meth:`set_cursor`, emitting ``CURSOR_CHANGED``) so the orchestrator can feed the preview without
poking the widget. Embedding runs off the injected worker scheduler (see
:meth:`set_worker_scheduler`): the VM owns the orchestration coroutine, the view supplies Textual's
``run_worker`` so the worker's lifecycle binds to the widget.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine

from rhizome.app.query_backed_model import QueryBackedViewModel
from rhizome.db import Resource
from rhizome.db.models import LoadingPreference, ResourceSection
from rhizome.db.operations import list_resources_for_topic
from rhizome.resources import ResourceLoadType, ResourceManager, ResourceTreeNodeKey

# A tree node is either a whole resource (root) or one of its sections (descendant).
ResourceTreeNodeData = Resource | ResourceSection


def _node_key(data: ResourceTreeNodeData) -> ResourceTreeNodeKey:
    """The load-state key for a tree node: ``("resource", id)`` or ``("section", id)``."""
    return ("resource", data.id) if isinstance(data, Resource) else ("section", data.id)


@dataclass(frozen=True)
class NodeLoadState:
    """Render facts for one tree node. The VM reports facts; the view maps them to glyph + colour.

    ``load_type`` is the type the node is *fully* loaded at, or ``None`` when it isn't fully loaded.
    ``partial`` is True when the node isn't fully loaded but some descendant is (a "half-check");
    ``load_type`` and ``partial`` are mutually exclusive (a fully-loaded node has no diverging
    descendants while the load state stays minimal). ``partial_has_context`` distinguishes the
    colour of a partial node — True iff *any* loaded descendant is ``CONTEXT`` (the view tints amber
    when even one descendant is context-loaded, green only when every loaded descendant is indexed).
    ``pending`` is True while the owning resource is computing embeddings (spinner + locked against
    toggles).
    """
    load_type: ResourceLoadType | None
    partial: bool
    partial_has_context: bool
    pending: bool


@dataclass(frozen=True)
class LoadStats:
    """Aggregate load picture for the status view, all counts over the *loaded* set.

    ``index_chunks`` / ``context_chunks`` split the loaded chunks by load type — a fully indexed vs
    context-stuffed breakdown (chunks shared across both fall to context). ``loaded_chunks`` is their
    sum. ``awaiting_embedding`` is the number of resources mid-embedding.
    """
    loaded_resources: int
    total_resources: int
    loaded_sections: int
    index_chunks: int
    context_chunks: int
    awaiting_embedding: int

    @property
    def loaded_chunks(self) -> int:
        return self.index_chunks + self.context_chunks


class _ResourceTreeIndex:
    """O(1) adjacency index over a topic's resources and their sections, rebuilt on every fetch.

    Treated as immutable between rebuilds. Provides the parent / child / descendant / ancestor walks
    the load-state algebra needs, keyed by ``ResourceTreeNodeKey``.
    """

    def __init__(self, resources: list[Resource]) -> None:
        self._resource_by_id: dict[int, Resource] = {}
        self._resource_id_by_section_id: dict[int, int] = {}
        self._parent_section_id: dict[int, int | None] = {}
        self._top_sections_by_resource_id: dict[int, list[ResourceSection]] = {}
        self._children_by_section_id: dict[int, list[ResourceSection]] = {}

        # Chunk ids per node, for the load-state chunk tally. A resource carries *all* its chunks;
        # a section carries the chunks mapped to it via the ``ResourceChunk``↔``ResourceSection`` M2M.
        self._chunk_ids_by_resource_id: dict[int, set[int]] = {}
        self._chunk_ids_by_section_id: dict[int, set[int]] = {}

        for r in resources:
            self._resource_by_id[r.id] = r
            self._chunk_ids_by_resource_id[r.id] = {c.id for c in (getattr(r, "chunks", None) or [])}
            top: list[ResourceSection] = []
            for s in getattr(r, "sections", None) or []:
                self._resource_id_by_section_id[s.id] = r.id
                self._parent_section_id[s.id] = s.parent_id
                self._chunk_ids_by_section_id[s.id] = {c.id for c in (getattr(s, "chunks", None) or [])}
                if s.parent_id is None:
                    top.append(s)
                else:
                    self._children_by_section_id.setdefault(s.parent_id, []).append(s)
            self._top_sections_by_resource_id[r.id] = top

    def resource_for_key(self, key: ResourceTreeNodeKey) -> Resource | None:
        rid = self.owning_resource_id(key)
        return self._resource_by_id.get(rid) if rid is not None else None

    def owning_resource_id(self, key: ResourceTreeNodeKey) -> int | None:
        kind, nid = key
        if kind == "resource":
            return nid if nid in self._resource_by_id else None
        return self._resource_id_by_section_id.get(nid)

    def parent_key(self, key: ResourceTreeNodeKey) -> ResourceTreeNodeKey | None:
        kind, nid = key
        if kind == "resource":
            return None
        # A top-level section's parent is its resource; a nested section's parent is another section.
        parent_section_id = self._parent_section_id.get(nid)
        if parent_section_id is not None:
            return ("section", parent_section_id)
        rid = self._resource_id_by_section_id.get(nid)
        return ("resource", rid) if rid is not None else None

    def child_keys(self, key: ResourceTreeNodeKey) -> list[ResourceTreeNodeKey]:
        kind, nid = key
        if kind == "resource":
            return [("section", s.id) for s in self._top_sections_by_resource_id.get(nid, [])]
        return [("section", s.id) for s in self._children_by_section_id.get(nid, [])]

    def descendant_keys(self, key: ResourceTreeNodeKey) -> list[ResourceTreeNodeKey]:
        result: list[ResourceTreeNodeKey] = []
        queue = list(self.child_keys(key))
        while queue:
            k = queue.pop()
            result.append(k)
            queue.extend(self.child_keys(k))
        return result

    def chunk_ids(self, key: ResourceTreeNodeKey) -> set[int]:
        """The node's *own* chunk ids — every chunk on a resource, or a section's mapped chunks."""
        kind, nid = key
        if kind == "resource":
            return self._chunk_ids_by_resource_id.get(nid, set())
        return self._chunk_ids_by_section_id.get(nid, set())


class ResourceLoaderModel(QueryBackedViewModel):
    """Loader VM. Owns the topic's resources, the load state, and the cursor mirror."""

    class Callbacks(Enum):
        # No payload — listeners read ``cursor_target``. Split from ``dirty`` so the preview feed
        # doesn't treat a highlight move as a load-state change.
        CURSOR_CHANGED = "cursor_changed"

    # The "auto" loading preference resolves by size: resources estimated at or below this many
    # tokens are context-stuffed, larger ones are indexed.
    AUTO_INDEX_TOKEN_THRESHOLD = 10_000

    def __init__(self, session_factory: Any, manager: ResourceManager) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._manager = manager

        self._cursor_changed = self._make_group(ResourceLoaderModel.Callbacks.CURSOR_CHANGED)

        # Topic scope. ``None`` = no active topic (empty tree).
        self._topic_id: int | None = None

        # Domain data for the current topic + the adjacency index derived from it.
        self._resources: list[Resource] = []
        self._index = _ResourceTreeIndex([])

        # Load state, kept minimal after every mutation. See module docstring.
        self._load_state: dict[ResourceTreeNodeKey, ResourceLoadType] = {}

        # Resources currently computing embeddings — spinner-rendered, locked against toggles, and
        # held back from the manager sync until completion.
        self._pending_resources: set[int] = set()

        # Mirror of the view's highlighted node. The cursor's source of truth is the widget.
        self._cursor_target: ResourceTreeNodeData | None = None

        # Scheduler hook for embedding workers. The view overrides this on mount with Textual's
        # ``run_worker``; the default keeps headless / test callers working.
        self._schedule_worker: Callable[[Coroutine[Any, Any, Any]], object] = asyncio.create_task

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def cursor_changed(self):
        return self._cursor_changed

    @property
    def resources(self) -> list[Resource]:
        return self._resources

    @property
    def cursor_target(self) -> ResourceTreeNodeData | None:
        return self._cursor_target

    def is_pending(self, resource_id: int) -> bool:
        return resource_id in self._pending_resources

    def node_load_state(self, data: ResourceTreeNodeData) -> NodeLoadState:
        """Render facts for one node — the single read the tree's ``render_label`` makes per row."""
        key = _node_key(data)
        effective = self._effective_type(key)
        partial = effective is None and self._has_descendant_entry(key)
        rid = self._index.owning_resource_id(key)
        return NodeLoadState(
            load_type=effective,
            partial=partial,
            partial_has_context=partial and self._descendant_entries_any_context(key),
            pending=rid is not None and rid in self._pending_resources,
        )

    def load_stats(self) -> LoadStats:
        """Aggregate the load picture for the status view. See :class:`LoadStats`.

        Walks the load-state entries plus their descendants: a node is loaded iff some ancestor-or-self
        carries an entry, so enumerating entries + their covered descendants visits each loaded node
        exactly once (minimality keeps entries from nesting). Each entry's covered chunks are tagged by
        its load type; a chunk shared across an index and a context entry falls to context. Pending
        resources count as loaded (their toggle has landed locally).
        """
        loaded_resources: set[int] = set(self._pending_resources)
        loaded_sections: set[int] = set()
        index_chunks: set[int] = set()
        context_chunks: set[int] = set()

        for key, load_type in self._load_state.items():
            kind, nid = key
            if kind == "resource":
                loaded_resources.add(nid)
            else:
                loaded_sections.add(nid)
                rid = self._index.owning_resource_id(key)
                if rid is not None:
                    loaded_resources.add(rid)

            covered = set(self._index.chunk_ids(key))
            for desc_kind, desc_id in self._index.descendant_keys(key):
                if desc_kind == "section":
                    loaded_sections.add(desc_id)
                covered |= self._index.chunk_ids((desc_kind, desc_id))
            bucket = context_chunks if load_type is ResourceLoadType.CONTEXT else index_chunks
            bucket |= covered

        index_chunks -= context_chunks  # context wins a chunk it shares with an index entry
        return LoadStats(
            loaded_resources=len(loaded_resources),
            total_resources=len(self._resources),
            loaded_sections=len(loaded_sections),
            index_chunks=len(index_chunks),
            context_chunks=len(context_chunks),
            awaiting_embedding=len(self._pending_resources),
        )

    # ------------------------------------------------------------------
    # Worker injection
    # ------------------------------------------------------------------

    def set_worker_scheduler(self, scheduler: Callable[[Coroutine[Any, Any, Any]], object]) -> None:
        """Inject the coroutine scheduler used for embedding workers. Called by the view on mount
        with Textual's ``run_worker``; tests / headless callers keep the ``asyncio`` default."""
        self._schedule_worker = scheduler

    # ------------------------------------------------------------------
    # Topic scope / fetch
    # ------------------------------------------------------------------

    def set_topic(self, topic_id: int | None) -> None:
        """Point the loader at a topic and refetch its resources. Identity-guarded."""
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        self._request_fetch()

    def reload(self) -> None:
        """Refetch the current topic's resources — e.g. after the linker commits a link change."""
        self._request_fetch()

    # ------------------------------------------------------------------
    # Cursor mirror (pushed by the view on highlight)
    # ------------------------------------------------------------------

    def set_cursor(self, target: ResourceTreeNodeData | None) -> None:
        """Mirror the view's highlighted node. Identity-guarded to kill the highlight↔repaint bounce;
        emits ``CURSOR_CHANGED`` (not ``dirty``) so only the preview feed reacts."""
        if target is self._cursor_target:
            return
        self._cursor_target = target
        self.emit(self._cursor_changed)

    # ------------------------------------------------------------------
    # Load mutators + the facts a view reads to drive them
    # ------------------------------------------------------------------
    #
    # The VM exposes "load this node at a type" and "unload this node", plus the two facts a keystroke
    # handler reads to decide which to call — the node's current type and its default type. Mapping a
    # key to a transition (space toggles the default, ctrl+j toggles CONTEXT, ...) is the view's job.

    def load(self, data: ResourceTreeNodeData, load_type: ResourceLoadType) -> None:
        """Load ``data``'s subtree at ``load_type``. No-op while the owning resource is mid-embedding."""
        self._set(data, load_type)

    def unload(self, data: ResourceTreeNodeData) -> None:
        """Unload ``data``'s subtree. No-op while the owning resource is mid-embedding."""
        self._set(data, None)

    def effective_type(self, data: ResourceTreeNodeData) -> ResourceLoadType | None:
        """The node's current load type, or ``None`` if it isn't fully loaded at a single type."""
        return self._effective_type(_node_key(data))

    def default_load_type(self, data: ResourceTreeNodeData) -> ResourceLoadType | None:
        """The type the owning resource's loading preference (+ token estimate) resolves to — the
        target a "default load" gesture loads at. ``None`` if the node has no owning resource."""
        return self._resolve_default_type(self._index.resource_for_key(_node_key(data)))

    def _set(self, data: ResourceTreeNodeData, target_type: ResourceLoadType | None) -> None:
        key = _node_key(data)
        resource = self._index.resource_for_key(key)

        # Pending resources are locked: their state is mid-flight to the manager, so ignore mutations
        # until the embedding worker resolves.
        if resource is None or resource.id in self._pending_resources:
            return

        self._apply(key, target_type)
        self.emit(self.dirty)

        # A load may introduce an INDEX entry that lacks embeddings; defer the manager sync until the
        # worker lands. Otherwise sync immediately.
        if self._needs_embeddings(resource):
            self._start_embedding(resource)
        else:
            self._sync_manager()

    # ------------------------------------------------------------------
    # Core algorithm: expand / clear / set / collapse over the load state
    # ------------------------------------------------------------------

    def _apply(self, key: ResourceTreeNodeKey, target_type: ResourceLoadType | None) -> None:
        """Set ``key``'s effective type to ``target_type`` (``None`` = unload), keeping the load state
        minimal. See module docstring for the four-step shape."""
        owner = self._entry_owner(key)

        # Expand: if the effective type is inherited from a proper ancestor, push that entry down the
        # ancestor→key path so ``key`` ends up with its own entry to mutate.
        if owner is not None and owner != key:
            self._expand_along_path(owner, key)

        # Clear: entries beneath ``key`` are about to be dominated by the type we set, so drop them.
        for descendant in self._index.descendant_keys(key):
            self._load_state.pop(descendant, None)

        # Set: write or remove this node's entry.
        if target_type is None:
            self._load_state.pop(key, None)
        else:
            self._load_state[key] = target_type

        # Collapse: promote agreeing siblings into their parent, walking up as far as it holds.
        self._collapse_upward(key)

    def _expand_along_path(self, ancestor: ResourceTreeNodeKey, target: ResourceTreeNodeKey) -> None:
        """Push ``ancestor``'s entry down to ``target``: walk the ancestor→target path top-down,
        replacing each interior node's entry with entries on its direct children at the same type,
        until ``target`` itself carries an entry."""
        # Build the path [ancestor, ..., target] by walking up from target.
        path: list[ResourceTreeNodeKey] = []
        cursor: ResourceTreeNodeKey | None = target
        while cursor is not None and cursor != ancestor:
            path.append(cursor)
            cursor = self._index.parent_key(cursor)
        if cursor is None:
            return  # ``ancestor`` wasn't actually an ancestor — safety net.
        path.append(ancestor)
        path.reverse()

        # Top-down, push each interior node's entry into its direct children.
        for node in path[:-1]:
            load_type = self._load_state.pop(node)
            for child in self._index.child_keys(node):
                self._load_state.setdefault(child, load_type)

    def _collapse_upward(self, key: ResourceTreeNodeKey) -> None:
        """Walk up from ``key``'s parent; whenever every child of a node carries an entry of the same
        type, replace them with a single entry on that node and continue. This is the rule that makes
        loading all of a node's children equivalent to loading the node."""
        parent = self._index.parent_key(key)
        while parent is not None:
            children = self._index.child_keys(parent)
            if not children:
                break
            child_types = {self._load_state.get(c) for c in children}
            if len(child_types) == 1 and None not in child_types:
                agreed = next(iter(child_types))
                for child in children:
                    self._load_state.pop(child, None)
                self._load_state[parent] = agreed  # type: ignore[assignment]
                parent = self._index.parent_key(parent)
            else:
                break

    # ------------------------------------------------------------------
    # Load-state reads
    # ------------------------------------------------------------------

    def _entry_owner(self, key: ResourceTreeNodeKey) -> ResourceTreeNodeKey | None:
        """The nearest ancestor-or-self of ``key`` that carries an entry, or ``None``."""
        cursor: ResourceTreeNodeKey | None = key
        while cursor is not None:
            if cursor in self._load_state:
                return cursor
            cursor = self._index.parent_key(cursor)
        return None

    def _effective_type(self, key: ResourceTreeNodeKey) -> ResourceLoadType | None:
        owner = self._entry_owner(key)
        return self._load_state[owner] if owner is not None else None

    def _has_descendant_entry(self, key: ResourceTreeNodeKey) -> bool:
        return any(d in self._load_state for d in self._index.descendant_keys(key))

    def _descendant_entries_any_context(self, key: ResourceTreeNodeKey) -> bool:
        return any(
            self._load_state[d] is ResourceLoadType.CONTEXT
            for d in self._index.descendant_keys(key)
            if d in self._load_state
        )

    # ------------------------------------------------------------------
    # Default-type resolution
    # ------------------------------------------------------------------

    def _resolve_default_type(self, resource: Resource | None) -> ResourceLoadType | None:
        """The type ``space`` maps to for a resource: explicit preference wins, else size decides."""
        if resource is None:
            return None
        pref = resource.loading_preference
        if pref == LoadingPreference.context_stuff:
            return ResourceLoadType.CONTEXT
        if pref == LoadingPreference.vector_store:
            return ResourceLoadType.INDEX
        tokens = resource.estimated_tokens or 0
        return (
            ResourceLoadType.CONTEXT
            if tokens <= self.AUTO_INDEX_TOKEN_THRESHOLD
            else ResourceLoadType.INDEX
        )

    # ------------------------------------------------------------------
    # Embedding lifecycle + manager sync
    # ------------------------------------------------------------------

    def _subtree_indexed(self, resource: Resource) -> bool:
        """True if any node in the resource's subtree is loaded at ``INDEX``."""
        root: ResourceTreeNodeKey = ("resource", resource.id)
        keys = [root, *self._index.descendant_keys(root)]
        return any(self._load_state.get(k) == ResourceLoadType.INDEX for k in keys)

    def _needs_embeddings(self, resource: Resource) -> bool:
        """True if the resource has an INDEX entry but lacks embeddings and isn't already computing."""
        if not self._subtree_indexed(resource):
            return False
        chunks = getattr(resource, "chunks", None) or []
        if chunks and all(c.embedding is not None for c in chunks):
            return False
        if self._manager.is_embedding_in_progress(resource.id):
            return False
        return resource.id not in self._pending_resources

    def _start_embedding(self, resource: Resource) -> None:
        """Mark the resource pending and spawn the embedding worker. Sync is deferred to completion."""
        self._pending_resources.add(resource.id)
        self.emit(self.dirty)
        self._schedule_worker(self._embed(resource))

    async def _embed(self, resource: Resource) -> None:
        """Worker: compute embeddings, then sync. On failure, roll back the resource's load state."""
        success = await self._manager.ensure_embedded(resource.id)
        self._pending_resources.discard(resource.id)
        if not success:
            self._apply(("resource", resource.id), None)
        self.emit(self.dirty)
        self._sync_manager()

    def _sync_manager(self) -> None:
        """Push the load state to the manager, holding back nodes whose resource is mid-embedding."""
        state = {
            key: load_type
            for key, load_type in self._load_state.items()
            if self._index.owning_resource_id(key) not in self._pending_resources
        }
        self._manager.set_state(state)

    # ------------------------------------------------------------------
    # QueryBackedViewModel contract
    # ------------------------------------------------------------------

    async def _fetch(self) -> list[Resource]:
        topic_id = self._topic_id
        if topic_id is None:
            return []
        async with self._session_factory() as session:
            return await list_resources_for_topic(session, topic_id, load_chunks=True)

    def _process_fetched_data(self, resources: list[Resource]) -> None:
        # Swap in the new resource set + index, then prune load state and pending for nodes that no
        # longer exist (a resource unlinked from the topic, a section deleted, etc.).
        self._resources = resources
        self._index = _ResourceTreeIndex(resources)
        valid: set[ResourceTreeNodeKey] = set()
        for r in resources:
            valid.add(("resource", r.id))
            for s in getattr(r, "sections", None) or []:
                valid.add(("section", s.id))
        self._load_state = {k: t for k, t in self._load_state.items() if k in valid}
        self._pending_resources &= {r.id for r in resources}
        self._sync_manager()



