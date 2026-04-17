"""ResourceLoader — tree-based widget for loading resources into the agent session.

Resources are root nodes; sections (if extracted) appear as expandable
children.  Both resources and sections can be toggled to LOADED or
CONTEXT_STUFFED modes via space / ctrl+enter respectively, with the
resource's ``loading_preference`` + token estimate deciding which concrete
mode ``space`` resolves to.

State is stored in MDL form: a flat ``dict[NodeKey, LoadMode]`` where an
entry means "this node and every descendant are in this mode, unless a
descendant has its own overriding entry."  The loader maintains the
invariant that no ancestor–descendant pair exists at the same mode (such
pairs are always collapsed to a single parent entry).

Toggle semantics:

- ``space`` resolves to the resource's *default* mode (LOADED or
  CONTEXT_STUFFED depending on ``loading_preference`` + token estimate).
  Pressing space cycles: effective-default → unloaded, anything-else →
  effective-default.
- ``ctrl+enter`` cycles: CONTEXT_STUFFED → unloaded, anything-else →
  CONTEXT_STUFFED.

The toggle handler applies three transformations to preserve the MDL
invariant: **expand** along the ancestor path if the effective mode comes
from a proper ancestor; **clear** the target's descendant entries (they
are now dominated by the target's new mode); **collapse** upward if the
target's siblings all agree on the same mode.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

from rhizome.db import Resource
from rhizome.db.models import LoadingPreference, ResourceSection
from rhizome.resources import LoadMode, NodeKey, ResourceManager

from rhizome.tui.dock import DockableWidgetMixin
from rhizome.tui.types import Arrangement
from rhizome.tui.widgets.resource.view_model import ResourceLoaderViewModel
from rhizome.tui.widgets.resource.loader_tree import (
    LoaderHint,
    LoaderTree,
    NodeData,
    _fmt_tokens,
    _owning_resource,
    _SPINNER_FRAMES,
    _state_key,
)


class _ResourceSectionCache:
    """O(1) lookup indexes over a list of resources and their sections.

    Rebuilt from scratch on every :meth:`ResourceLoader.set_resources` call;
    treat as immutable between rebuilds.  Any DB commit that changes the
    resource list should flow through ``set_resources`` (via the refresh
    path triggered by ``RhizomeApp.on_data_changed``), which rebuilds this
    cache automatically.
    """

    def __init__(self, resources: list[Resource]) -> None:
        self.resources: list[Resource] = resources
        self.resource_by_id: dict[int, Resource] = {}
        self.section_by_id: dict[int, ResourceSection] = {}
        self.resource_id_by_section_id: dict[int, int] = {}
        self.all_sections_by_resource_id: dict[int, list[ResourceSection]] = {}
        self.top_sections_by_resource_id: dict[int, list[ResourceSection]] = {}
        self.children_by_section_id: dict[int, list[ResourceSection]] = {}

        for r in resources:
            self.resource_by_id[r.id] = r
            sections = list(getattr(r, "sections", None) or [])
            self.all_sections_by_resource_id[r.id] = sections
            top: list[ResourceSection] = []
            for s in sections:
                self.section_by_id[s.id] = s
                self.resource_id_by_section_id[s.id] = r.id
                if s.parent_id is None:
                    top.append(s)
                else:
                    self.children_by_section_id.setdefault(s.parent_id, []).append(s)
            self.top_sections_by_resource_id[r.id] = top

    # -- Lookup primitives (O(1) or O(children)) -----------------------

    def resource_for_key(self, key: NodeKey) -> Resource | None:
        kind, nid = key
        if kind == "resource":
            return self.resource_by_id.get(nid)
        rid = self.resource_id_by_section_id.get(nid)
        return self.resource_by_id.get(rid) if rid is not None else None

    def owning_resource_id(self, key: NodeKey) -> int | None:
        kind, nid = key
        if kind == "resource":
            return nid if nid in self.resource_by_id else None
        return self.resource_id_by_section_id.get(nid)

    def parent_key(self, key: NodeKey) -> NodeKey | None:
        kind, nid = key
        if kind == "resource":
            return None
        s = self.section_by_id.get(nid)
        if s is None:
            return None
        if s.parent_id is None:
            rid = self.resource_id_by_section_id.get(nid)
            return ("resource", rid) if rid is not None else None
        return ("section", s.parent_id)

    def child_keys(self, key: NodeKey) -> list[NodeKey]:
        kind, nid = key
        if kind == "resource":
            return [("section", s.id) for s in self.top_sections_by_resource_id.get(nid, [])]
        return [("section", s.id) for s in self.children_by_section_id.get(nid, [])]

    def descendant_keys(self, key: NodeKey) -> list[NodeKey]:
        result: list[NodeKey] = []
        queue = list(self.child_keys(key))
        while queue:
            k = queue.pop()
            result.append(k)
            queue.extend(self.child_keys(k))
        return result


class ResourceLoader(Widget, DockableWidgetMixin, can_focus=True):
    """Container widget with an inner tree and a status hint.

    Delegates focus to the inner ``LoaderTree`` and exposes the same
    public API that ``ResourceViewer`` expects.
    """

    BINDINGS = [
        Binding("space", "toggle_default", show=False),
        Binding("ctrl+j", "toggle_context", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ResourceLoader {
        height: auto;
        layout: vertical;
    }
    ResourceLoader #rld-hint {
        color: rgb(80,80,80);
        margin: 0 0 0 2;
    }
    ResourceLoader #rld-detail {
        display: none;
        height: auto;
        color: rgb(100,100,100);
        margin: 0 1 0 2;
    }
    ResourceLoader #rld-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 2;
    }
    """

    # Token threshold for the "auto" loading preference: resources below
    # this estimate are context-stuffed, above are vector-embedded.
    AUTO_CONTEXT_STUFF_TOKEN_LIMIT = 10_000

    # -- Reactives -----------------------------------------------------

    show_ids: reactive[bool] = reactive(False)

    # -- Init / compose ------------------------------------------------

    def __init__(
        self,
        view_model: ResourceLoaderViewModel | None = None,
        resource_manager: ResourceManager | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model or ResourceLoaderViewModel()
        self._resource_manager = resource_manager
        self._spinner_timer = None
        self._cache = _ResourceSectionCache(self._vm.resources)

    # -- Properties that read/write through to the view model -------------

    @property
    def _resources(self) -> list[Resource]:
        return self._vm.resources

    @_resources.setter
    def _resources(self, value: list[Resource]) -> None:
        self._vm.resources = value

    @property
    def _states(self) -> dict[NodeKey, LoadMode]:
        return self._vm.states

    @_states.setter
    def _states(self, value: dict[NodeKey, LoadMode]) -> None:
        self._vm.states = value

    @property
    def _pending_resources(self) -> set[int]:
        return self._vm.pending_resources

    @property
    def _spinner_frame(self) -> int:
        return self._vm.spinner_frame

    @_spinner_frame.setter
    def _spinner_frame(self, value: int) -> None:
        self._vm.spinner_frame = value

    def compose(self) -> ComposeResult:
        yield Static("", id="rld-empty")
        yield Static("", id="rld-detail")
        yield LoaderTree(self, id="rld-tree")
        yield LoaderHint(id="rld-hint")

    def on_mount(self) -> None:
        self.show_ids = self._vm.show_ids
        self.query_one("#rld-hint", LoaderHint).vertical = (
            self.dock_arrangement == Arrangement.VERTICAL
        )
        self._spinner_timer = self.set_interval(0.1, self._tick_spinner, pause=True)
        self._apply_empty_state()
        self._update_spinner_timer()
        if self._resources:
            self._update_hint()
        if self.dock_arrangement == Arrangement.VERTICAL:
            self.call_after_refresh(self._constrain_tree)

    def on_resize(self) -> None:
        if self.dock_arrangement == Arrangement.VERTICAL:
            self._constrain_tree()

    def _constrain_tree(self) -> None:
        tree = self._tree
        siblings_height = sum(
            child.size.height for child in self.children if child is not tree
        )
        available = self.size.height - siblings_height - 6
        if available > 0:
            tree.styles.max_height = available

    def _tick_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        self._tree._invalidate_label_cache()

    def _update_spinner_timer(self) -> None:
        if self._spinner_timer is not None:
            if self._pending_resources:
                self._spinner_timer.resume()
            else:
                self._spinner_timer.pause()

    @property
    def _tree(self) -> LoaderTree:
        return self.query_one("#rld-tree", LoaderTree)

    # -- Focus delegation ----------------------------------------------

    def focus(self, scroll_visible: bool = True) -> Widget:
        """Delegate focus to the inner tree."""
        return self._tree.focus(scroll_visible)

    # -- Tree navigation helpers (all delegate to the cache) -----------

    def _ancestor_entry_owner(self, key: NodeKey) -> NodeKey | None:
        """Return the nearest ancestor-or-self of ``key`` with an entry, or None."""
        current: NodeKey | None = key
        while current is not None:
            if current in self._states:
                return current
            current = self._cache.parent_key(current)
        return None

    def _effective_mode(self, key: NodeKey) -> LoadMode | None:
        """The effective mode of ``key`` given the current MDL state."""
        owner = self._ancestor_entry_owner(key)
        return self._states[owner] if owner is not None else None

    def _has_descendant_entry(self, key: NodeKey) -> bool:
        """True if any descendant of ``key`` has its own entry."""
        return any(k in self._states for k in self._cache.descendant_keys(key))

    def _is_partially_loaded(self, data: NodeData) -> bool:
        """True if some — but not all — of *data*'s descendants share its effective mode."""
        key = _state_key(data)
        if not self._cache.child_keys(key):
            return False
        return self._has_descendant_entry(key)

    def _descendant_modes_only_cs(self, data: NodeData) -> bool:
        """True iff every descendant entry under *data* is CONTEXT_STUFFED.

        Used for checkbox color when a node has no effective mode itself but
        does have descendant entries — pick amber only if they're all CS.
        """
        key = _state_key(data)
        descendant_entries = [
            self._states[k] for k in self._cache.descendant_keys(key) if k in self._states
        ]
        if not descendant_entries:
            return False
        return all(m == LoadMode.CONTEXT_STUFFED for m in descendant_entries)

    # -- Public API ----------------------------------------------------

    def set_resources(self, resources: list[Resource]) -> None:
        """Replace the tree contents with a new list of resources.

        Rebuilds the section cache and prunes stale state entries for
        resources/sections that no longer exist.  This is the single entry
        point for resource-list changes; DB commits flow here via the
        refresh path in :class:`RhizomeApp.on_data_changed`.
        """
        self._resources = list(resources)
        self._cache = _ResourceSectionCache(self._resources)

        valid_keys: set[NodeKey] = set()
        for r in resources:
            valid_keys.add(("resource", r.id))
            for s in getattr(r, "sections", None) or []:
                valid_keys.add(("section", s.id))
        for k in [k for k in self._states if k not in valid_keys]:
            del self._states[k]
        self._pending_resources.intersection_update(r.id for r in resources)

        tree = self._tree
        tree.root.remove_children()
        for resource in resources:
            has_sections = bool(getattr(resource, "sections", None))
            if has_sections:
                tree.root.add(resource.name, data=resource, allow_expand=True)
            else:
                tree.root.add_leaf(resource.name, data=resource)
        tree._refresh_height()
        if tree.root.children:
            tree.move_cursor(tree.root.children[0])
        self._apply_empty_state()
        self._update_hint()

    def is_pending(self, resource_id: int) -> bool:
        """True if the resource is computing embeddings."""
        return resource_id in self._pending_resources

    # -- Reactive watchers ---------------------------------------------

    def watch_show_ids(self) -> None:
        self._vm.show_ids = self.show_ids
        tree = self._tree
        tree._invalidate_label_cache()
        tree.styles.padding = (0, 2, 0, 0) if self.show_ids else (0, 0, 0, 0)
        cursor_node = tree.cursor_node
        if cursor_node is not None:
            self._update_detail(cursor_node.data)

    # -- Rendering -----------------------------------------------------

    def _apply_empty_state(self) -> None:
        empty = not self._resources
        self.query_one("#rld-empty", Static).display = empty
        self._tree.display = not empty
        self.query_one("#rld-hint", LoaderHint).display = not empty
        if empty:
            self.query_one("#rld-empty", Static).update("(No resources linked to this topic)")
            detail = self.query_one("#rld-detail", Static)
            detail.update("")
            detail.display = False

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[NodeData]) -> None:
        self._update_detail(event.node.data)

    def _update_detail(self, data: NodeData | None) -> None:
        """Update the detail panel with metadata for the highlighted node."""
        detail = self.query_one("#rld-detail", Static)
        if data is None:
            detail.update("")
            detail.display = False
            return

        if isinstance(data, Resource):
            parts: list[str] = []
            parts.append(f"~{_fmt_tokens(data.estimated_tokens)} tokens")
            try:
                chunk_count = len(data.chunks) if data.chunks is not None else 0
            except Exception:
                chunk_count = 0
            parts.append(f"{chunk_count} chunks")
            pref = data.loading_preference.value if data.loading_preference else "—"
            parts.append(pref)
            if self.show_ids:
                parts.append(f"id: {data.id}")
            detail.update(" │ ".join(parts))
            detail.display = True
        elif isinstance(data, ResourceSection):
            parts: list[str] = []
            try:
                chunk_count = len(data.chunks) if data.chunks is not None else 0
            except Exception:
                chunk_count = 0
            parts.append(f"{chunk_count} chunks")
            if self.show_ids:
                parts.append(f"id: {data.id}")
            detail.update(" │ ".join(parts))
            detail.display = True
        else:
            detail.update("")
            detail.display = False

    def _update_hint(self) -> None:
        """Recompute the "N/M loaded, K sections" summary.

        Iterates MDL entries directly rather than every node: under the MDL
        invariant, a node has an effective mode iff some ancestor-or-self has
        an entry, so enumerating `_states` + their descendants (via the
        cache) covers every loaded node exactly once.
        """
        loaded_resources: set[int] = set(self._pending_resources)
        loaded_sections: set[int] = set()
        for key in self._states:
            kind, nid = key
            if kind == "resource":
                loaded_resources.add(nid)
                for desc in self._cache.descendant_keys(key):
                    loaded_sections.add(desc[1])
            else:
                loaded_sections.add(nid)
                for desc in self._cache.descendant_keys(key):
                    loaded_sections.add(desc[1])
                rid = self._cache.owning_resource_id(key)
                if rid is not None:
                    loaded_resources.add(rid)
        hint = self.query_one("#rld-hint", LoaderHint)
        hint.loaded = len(loaded_resources)
        hint.total = len(self._resources)
        hint.sections = len(loaded_sections)

    # -- Toggle (expand / set / collapse) ------------------------------

    def _expand_along_path(self, ancestor: NodeKey, target: NodeKey) -> None:
        """Push ``ancestor``'s entry down so ``target`` gets its own entry.

        Walks from ``ancestor`` down to ``target``.  At each step, takes the
        current node's mode, removes its entry, and materializes entries for
        each of its direct children at that mode.  Repeats until ``target``
        itself has an entry.
        """
        if ancestor == target:
            return

        # Build the ancestor→target path (exclusive of target, inclusive of ancestor).
        path: list[NodeKey] = []
        cursor: NodeKey | None = target
        while cursor is not None and cursor != ancestor:
            path.append(cursor)
            cursor = self._cache.parent_key(cursor)
        if cursor is None:
            return  # ancestor is not actually an ancestor — safety net
        path.append(ancestor)
        path.reverse()  # now [ancestor, ..., target's parent, target]

        # Walk the path top-down.  At each interior node, push its entry into
        # its direct children.
        for i in range(len(path) - 1):
            node = path[i]
            mode = self._states.pop(node)
            for child in self._cache.child_keys(node):
                if child not in self._states:
                    self._states[child] = mode

    def _collapse_upward(self, key: NodeKey) -> None:
        """Walk up from ``key``'s parent, promoting whenever all siblings agree.

        At each parent P: if every direct child of P has an entry and all
        those entries share the same mode M, remove them all and set
        ``_states[P] = M``.  Continue to P's parent.
        """
        parent = self._cache.parent_key(key)
        while parent is not None:
            children = self._cache.child_keys(parent)
            if not children:
                break
            child_modes = {self._states.get(c) for c in children}
            if len(child_modes) == 1 and None not in child_modes:
                agreed = next(iter(child_modes))
                for c in children:
                    self._states.pop(c, None)
                self._states[parent] = agreed  # type: ignore[assignment]
                parent = self._cache.parent_key(parent)
            else:
                break

    def _apply_toggle(self, key: NodeKey, new_mode: LoadMode | None) -> None:
        """Set ``key``'s effective mode to ``new_mode`` (None = unload).

        Preserves MDL invariant via expand / clear-descendants / set / collapse.
        """
        owner = self._ancestor_entry_owner(key)

        # Step 1: if the effective mode comes from a proper ancestor, push it
        # down along the path so ``key`` gets its own entry.
        if owner is not None and owner != key:
            self._expand_along_path(owner, key)

        # Step 2: clear any entries beneath ``key`` (they're now dominated by
        # the new mode we're about to set).
        for desc in self._cache.descendant_keys(key):
            self._states.pop(desc, None)

        # Step 3: set or remove this node's entry.
        if new_mode is None:
            self._states.pop(key, None)
        else:
            self._states[key] = new_mode

        # Step 4: collapse upward if siblings now agree.
        self._collapse_upward(key)

    # -- Embedding & manager sync --------------------------------------

    def _resolve_default_mode(self, resource: Resource) -> LoadMode:
        """The concrete mode that ``space`` maps to for this resource."""
        pref = resource.loading_preference
        tokens = resource.estimated_tokens or 0
        if pref == LoadingPreference.context_stuff:
            return LoadMode.CONTEXT_STUFFED
        if pref == LoadingPreference.vector_store:
            return LoadMode.LOADED
        return (
            LoadMode.CONTEXT_STUFFED
            if tokens <= self.AUTO_CONTEXT_STUFF_TOKEN_LIMIT
            else LoadMode.LOADED
        )

    def _resource_has_loaded_entry(self, resource: Resource) -> bool:
        """True if any entry in this resource's subtree is in LOADED mode."""
        root: NodeKey = ("resource", resource.id)
        keys = [root, *self._cache.descendant_keys(root)]
        return any(self._states.get(k) == LoadMode.LOADED for k in keys)

    def _needs_embeddings(self, resource: Resource) -> bool:
        """True if the resource has at least one LOADED entry and lacks embeddings."""
        if not self._resource_has_loaded_entry(resource):
            return False
        chunks = getattr(resource, "chunks", None) or []
        if chunks and all(c.embedding is not None for c in chunks):
            return False
        if self._resource_manager is not None and self._resource_manager.is_embedding_in_progress(resource.id):
            return False
        return True

    def _start_embedding(self, resource: Resource) -> None:
        """Mark the resource pending and start an embedding worker."""
        self._pending_resources.add(resource.id)
        self._tree._invalidate_label_cache()
        self._update_spinner_timer()
        self._update_hint()
        self.run_worker(self._compute_embeddings(resource), exclusive=False)

    async def _compute_embeddings(self, resource: Resource) -> None:
        """Worker coroutine: compute embeddings, then resolve."""
        if self._resource_manager is None:
            success = False
        else:
            success = await self._resource_manager.ensure_embedded(resource.id)

        self._pending_resources.discard(resource.id)
        if not success:
            # Roll back the user's intent: remove any state under this resource.
            resource_key: NodeKey = ("resource", resource.id)
            self._states.pop(resource_key, None)
            for desc in self._cache.descendant_keys(resource_key):
                self._states.pop(desc, None)

        self._tree._invalidate_label_cache()
        self._update_spinner_timer()
        self._update_hint()
        self._sync_manager_state()

    def _sync_manager_state(self) -> None:
        """Push the current MDL state to the manager, filtering pending resources.

        Pending resources are held back: their entries exist in the loader
        (preserving user intent) but are not propagated to the manager until
        embedding completes.
        """
        if self._resource_manager is None:
            return

        filtered: dict[NodeKey, LoadMode] = {
            k: v
            for k, v in self._states.items()
            if self._cache.owning_resource_id(k) not in self._pending_resources
        }
        self._resource_manager.set_state(filtered)

    # -- Actions -------------------------------------------------------

    def _is_locked(self, node: TreeNode[NodeData]) -> bool:
        """True if the node (or its owning resource) is pending."""
        data = node.data
        if data is None:
            return False
        if isinstance(data, Resource):
            return data.id in self._pending_resources
        if isinstance(data, ResourceSection):
            return data.resource_id in self._pending_resources
        return False

    def _cursor_key(self) -> tuple[TreeNode[NodeData], NodeKey, Resource] | None:
        node = self._tree.cursor_node
        if node is None or node.data is None:
            return None
        if self._is_locked(node):
            return None
        key = _state_key(node.data)
        resource = self._cache.resource_for_key(key)
        if resource is None:
            return None
        return node, key, resource

    def action_toggle_default(self) -> None:
        """space: cycle between "default mode" and unloaded."""
        cursor = self._cursor_key()
        if cursor is None:
            return
        _, key, resource = cursor
        default_mode = self._resolve_default_mode(resource)
        current_effective = self._effective_mode(key)

        if current_effective == default_mode:
            target_mode: LoadMode | None = None
        else:
            target_mode = default_mode

        self._toggle(key, resource, target_mode)

    def action_toggle_context(self) -> None:
        """ctrl+j: cycle between CONTEXT_STUFFED and unloaded."""
        cursor = self._cursor_key()
        if cursor is None:
            return
        _, key, resource = cursor
        current_effective = self._effective_mode(key)

        if current_effective == LoadMode.CONTEXT_STUFFED:
            target_mode: LoadMode | None = None
        else:
            target_mode = LoadMode.CONTEXT_STUFFED

        self._toggle(key, resource, target_mode)

    def _toggle(
        self,
        key: NodeKey,
        resource: Resource,
        target_mode: LoadMode | None,
    ) -> None:
        """Common toggle path: apply the MDL transformation and sync."""
        self._apply_toggle(key, target_mode)
        self._tree._invalidate_label_cache()
        self._update_hint()

        # If the resource now has LOADED entries but lacks embeddings, kick
        # off the embedding worker and defer sync until it completes.
        if self._needs_embeddings(resource) and resource.id not in self._pending_resources:
            self._start_embedding(resource)
        else:
            self._sync_manager_state()
