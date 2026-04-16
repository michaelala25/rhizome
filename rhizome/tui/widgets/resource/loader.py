"""ResourceLoader — tree-based widget for loading resources into the agent session.

Resources are root nodes; sections (if extracted) appear as expandable
children.  Both resources and sections have independent load states
governed by the same state machine:

    unloaded  →(space)→  default  →(space)→  unloaded
    unloaded  →(ctrl+enter)→  context-stuffed  →(ctrl+enter)→  unloaded
    default   →(ctrl+enter)→  context-stuffed
    context-stuffed  →(space)→  default
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

from rhizome.db import Resource
from rhizome.db.models import LoadingPreference, ResourceSection
from rhizome.resources import LoadMode, ResourceLoadState, ResourceManager

from rhizome.tui.dock import DockableWidgetMixin
from rhizome.tui.types import Arrangement
from rhizome.tui.widgets.resource.view_model import LoadState, ResourceLoaderViewModel
from rhizome.tui.widgets.resource.loader_tree import (
    LoaderHint,
    LoaderTree,
    NodeData,
    _fmt_tokens,
    _owning_resource,
    _SPINNER_FRAMES,
    _state_key,
)


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

    # -- Properties that read/write through to the view model -------------

    @property
    def _resources(self) -> list[Resource]:
        return self._vm.resources

    @_resources.setter
    def _resources(self, value: list[Resource]) -> None:
        self._vm.resources = value

    @property
    def _states(self) -> dict[tuple[str, int], LoadState]:
        return self._vm.states

    @_states.setter
    def _states(self, value: dict[tuple[str, int], LoadState]) -> None:
        self._vm.states = value

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
        has_pending = any(s == LoadState.PENDING for s in self._states.values())
        if self._spinner_timer is not None:
            if has_pending:
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

    # -- Helpers -------------------------------------------------------

    def _is_partially_loaded(self, data: NodeData) -> bool:
        """True if the node has children and not all share the same load state."""
        if isinstance(data, Resource):
            sections = getattr(data, "sections", None) or []
            if not sections:
                return False
            parent_state = self._states.get(("resource", data.id), LoadState.UNLOADED)
            return any(
                self._states.get(("section", s.id), LoadState.UNLOADED) != parent_state
                for s in sections
            )
        elif isinstance(data, ResourceSection):
            resource = next((r for r in self._resources if r.id == data.resource_id), None)
            if resource is None:
                return False
            all_sections = getattr(resource, "sections", None) or []
            children = [s for s in all_sections if s.parent_id == data.id]
            if not children:
                return False
            parent_state = self._states.get(("section", data.id), LoadState.UNLOADED)
            return any(
                self._states.get(("section", c.id), LoadState.UNLOADED) != parent_state
                for c in children
            )
        return False

    # -- Public API ----------------------------------------------------

    def set_resources(self, resources: list[Resource]) -> None:
        """Replace the tree contents with a new list of resources."""
        self._resources = list(resources)

        # Prune stale load states for resources/sections that no longer exist.
        valid_keys: set[tuple[str, int]] = set()
        for r in resources:
            valid_keys.add(("resource", r.id))
            for s in getattr(r, "sections", None) or []:
                valid_keys.add(("section", s.id))
        stale = [k for k in self._states if k not in valid_keys]
        for k in stale:
            del self._states[k]

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

    def get_state(self, resource_id: int) -> LoadState:
        """Return the current load state for a resource."""
        return self._states.get(("resource", resource_id), LoadState.UNLOADED)

    def set_pending(self, resource_id: int) -> None:
        """Set a resource to PENDING state (embedding in progress)."""
        self._states[("resource", resource_id)] = LoadState.PENDING
        self._tree._invalidate_label_cache()
        self._update_spinner_timer()
        self._update_hint()

    def resolve_pending(self, resource_id: int, success: bool) -> None:
        """Resolve a pending resource: DEFAULT on success, UNLOADED on failure."""
        key = ("resource", resource_id)
        if self._states.get(key) != LoadState.PENDING:
            return
        if success:
            self._states[key] = LoadState.DEFAULT
        else:
            self._states.pop(key, None)
            # Failure: also revert all descendant section states that were
            # propagated when the resource was originally toggled.
            resource = next((r for r in self._resources if r.id == resource_id), None)
            if resource is not None:
                for s in getattr(resource, "sections", None) or []:
                    self._states.pop(("section", s.id), None)
        self._tree._invalidate_label_cache()
        self._update_spinner_timer()
        self._update_hint()

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
        loaded_count = 0
        total_sections = 0
        for resource in self._resources:
            res_state = self._states.get(("resource", resource.id), LoadState.UNLOADED)
            if res_state in (LoadState.DEFAULT, LoadState.CONTEXT_STUFFED, LoadState.PENDING):
                loaded_count += 1
            sections = getattr(resource, "sections", None) or []
            for section in sections:
                sec_state = self._states.get(("section", section.id), LoadState.UNLOADED)
                if sec_state in (LoadState.DEFAULT, LoadState.CONTEXT_STUFFED):
                    total_sections += 1
        hint = self.query_one("#rld-hint", LoaderHint)
        hint.loaded = loaded_count
        hint.total = len(self._resources)
        hint.sections = total_sections

    # -- State transitions ---------------------------------------------

    def _set_state(self, node: TreeNode[NodeData], new_state: LoadState) -> None:
        data = node.data
        if data is None:
            return
        key = _state_key(data)
        if new_state == LoadState.UNLOADED:
            self._states.pop(key, None)
        else:
            self._states[key] = new_state

        # Propagate to all descendant sections.
        self._propagate_to_descendants(data, new_state)

        # Propagate upward: if all siblings now share the same state, promote
        # that state to the parent (recursively up to the resource root).
        if isinstance(data, ResourceSection):
            self._propagate_state_to_ancestors(data)

        self._tree._invalidate_label_cache()
        self._update_spinner_timer()
        self._update_hint()

        # If a resource just entered DEFAULT and needs embeddings, go
        # PENDING and start a worker.  Otherwise sync state immediately.
        if new_state == LoadState.DEFAULT and isinstance(data, Resource):
            if self._needs_embeddings(data):
                self._start_embedding(data)
                return

        self._sync_manager_state()

    def _propagate_to_descendants(self, data: NodeData, new_state: LoadState) -> None:
        """Apply *new_state* to all descendant sections of *data*, skipping PENDING."""
        if isinstance(data, Resource):
            sections = getattr(data, "sections", None) or []
        elif isinstance(data, ResourceSection):
            resource = next((r for r in self._resources if r.id == data.resource_id), None)
            if resource is None:
                return
            all_sections = getattr(resource, "sections", None) or []
            sections = []
            queue = [s for s in all_sections if s.parent_id == data.id]
            while queue:
                s = queue.pop(0)
                sections.append(s)
                queue.extend(c for c in all_sections if c.parent_id == s.id)
        else:
            return

        for section in sections:
            key = ("section", section.id)
            if self._states.get(key) == LoadState.PENDING:
                continue
            if new_state == LoadState.UNLOADED:
                self._states.pop(key, None)
            else:
                self._states[key] = new_state

    def _propagate_state_to_ancestors(self, data: NodeData) -> None:
        """Walk up from *data*: if all children of a parent share the same
        state, promote that state to the parent.  Repeats up to the resource
        root.  PENDING nodes are never overwritten.
        """
        if not isinstance(data, ResourceSection):
            return
        resource = next((r for r in self._resources if r.id == data.resource_id), None)
        if resource is None:
            return
        all_sections = getattr(resource, "sections", None) or []

        # Walk up through section parents.
        current_parent_id = data.parent_id
        while current_parent_id is not None:
            children = [s for s in all_sections if s.parent_id == current_parent_id]
            child_states = {self._states.get(("section", s.id), LoadState.UNLOADED) for s in children}
            if len(child_states) == 1:
                agreed = next(iter(child_states))
                parent_key = ("section", current_parent_id)
                if self._states.get(parent_key) != LoadState.PENDING:
                    if agreed == LoadState.UNLOADED:
                        self._states.pop(parent_key, None)
                    else:
                        self._states[parent_key] = agreed
                parent_section = next((s for s in all_sections if s.id == current_parent_id), None)
                current_parent_id = parent_section.parent_id if parent_section else None
            else:
                break

        # Check whether all top-level sections agree → promote to resource root.
        top_sections = [s for s in all_sections if s.parent_id is None]
        top_states = {self._states.get(("section", s.id), LoadState.UNLOADED) for s in top_sections}
        if len(top_states) == 1:
            agreed = next(iter(top_states))
            res_key = ("resource", resource.id)
            if self._states.get(res_key) != LoadState.PENDING:
                if agreed == LoadState.UNLOADED:
                    self._states.pop(res_key, None)
                else:
                    self._states[res_key] = agreed

    # -- Embedding & manager sync --------------------------------------

    def _resolve_load_mode(self, resource: Resource, load_state: LoadState) -> LoadMode | None:
        """Map a loader LoadState to a manager LoadMode."""
        if load_state == LoadState.CONTEXT_STUFFED:
            return LoadMode.CONTEXT_STUFFED
        if load_state == LoadState.DEFAULT:
            pref = resource.loading_preference
            tokens = resource.estimated_tokens or 0
            if pref == LoadingPreference.context_stuff:
                return LoadMode.CONTEXT_STUFFED
            elif pref == LoadingPreference.vector_store:
                return LoadMode.LOADED
            else:
                return LoadMode.CONTEXT_STUFFED if tokens <= self.AUTO_CONTEXT_STUFF_TOKEN_LIMIT else LoadMode.LOADED
        return None

    def _needs_embeddings(self, resource: Resource) -> bool:
        """True if the resource resolves to LOADED and doesn't have embeddings yet."""
        mode = self._resolve_load_mode(resource, LoadState.DEFAULT)
        if mode != LoadMode.LOADED:
            return False
        chunks = getattr(resource, "chunks", None) or []
        if chunks and all(c.embedding is not None for c in chunks):
            return False
        if self._resource_manager is not None and self._resource_manager.is_embedding_in_progress(resource.id):
            return False
        return True

    def _start_embedding(self, resource: Resource) -> None:
        """Set the resource to PENDING and start an embedding worker."""
        self.set_pending(resource.id)
        self.run_worker(self._compute_embeddings(resource), exclusive=False)

    async def _compute_embeddings(self, resource: Resource) -> None:
        """Worker coroutine: compute embeddings, then resolve and sync."""
        if self._resource_manager is None:
            self.resolve_pending(resource.id, False)
            return
        success = await self._resource_manager.ensure_embedded(resource.id)
        self.resolve_pending(resource.id, success)
        self._sync_manager_state()

    def _sync_manager_state(self) -> None:
        """Build ResourceLoadState from loader states and push to the manager.

        Contract: for a resource with sections, if every section resolves to
        the same LoadMode (and none are unloaded), the state is collapsed to
        ``root_state=<mode>, sections={}``.  Otherwise ``root_state`` is
        ``None`` and only the per-section modes are provided.  For a resource
        without sections, ``root_state`` is the resolved mode directly.
        """
        if self._resource_manager is None:
            return

        resource_map: dict[int, ResourceLoadState] = {}

        for resource in self._resources:
            sections = getattr(resource, "sections", None) or []

            if not sections:
                # No sections — root state only.
                load_state = self._states.get(("resource", resource.id), LoadState.UNLOADED)
                if load_state in (LoadState.UNLOADED, LoadState.PENDING):
                    continue
                mode = self._resolve_load_mode(resource, load_state)
                if mode is not None:
                    resource_map[resource.id] = ResourceLoadState(root_state=mode, sections={})
            else:
                # Has sections — resolve each, then check uniformity.
                section_modes: dict[int, LoadMode | None] = {}
                for s in sections:
                    sec_state = self._states.get(("section", s.id), LoadState.UNLOADED)
                    if sec_state in (LoadState.UNLOADED, LoadState.PENDING):
                        section_modes[s.id] = None
                    else:
                        section_modes[s.id] = self._resolve_load_mode(resource, sec_state)

                active = {sid: m for sid, m in section_modes.items() if m is not None}
                if not active:
                    continue

                unique_modes = set(active.values())
                if len(unique_modes) == 1 and len(active) == len(sections):
                    # Every section agrees on the same mode — collapse to root.
                    resource_map[resource.id] = ResourceLoadState(
                        root_state=next(iter(unique_modes)),
                        sections={},
                    )
                else:
                    # Divergent — per-section detail, root is None.
                    resource_map[resource.id] = ResourceLoadState(
                        root_state=None,
                        sections=active,
                    )

        self._resource_manager.set_state(resource_map)

    # -- Actions -------------------------------------------------------

    def _is_resource_pending(self, node: TreeNode[NodeData]) -> bool:
        """True if the node's owning resource is in PENDING state."""
        data = node.data
        if isinstance(data, ResourceSection):
            resource = _owning_resource(node)
            return self._states.get(("resource", resource.id), LoadState.UNLOADED) == LoadState.PENDING
        return False

    def action_toggle_default(self) -> None:
        """space: unloaded <-> default, or context-stuffed -> default."""
        node = self._tree.cursor_node
        if node is None or node.data is None:
            return
        state = self._states.get(_state_key(node.data), LoadState.UNLOADED)
        if state == LoadState.PENDING or self._is_resource_pending(node):
            return
        if state == LoadState.UNLOADED:
            self._set_state(node, LoadState.DEFAULT)
        elif state == LoadState.DEFAULT:
            self._set_state(node, LoadState.UNLOADED)
        else:  # CONTEXT_STUFFED -> DEFAULT
            self._set_state(node, LoadState.DEFAULT)

    def action_toggle_context(self) -> None:
        """ctrl+enter: cycle context-stuffed."""
        node = self._tree.cursor_node
        if node is None or node.data is None:
            return
        state = self._states.get(_state_key(node.data), LoadState.UNLOADED)
        if state == LoadState.PENDING or self._is_resource_pending(node):
            return
        if state == LoadState.UNLOADED:
            self._set_state(node, LoadState.CONTEXT_STUFFED)
        elif state == LoadState.DEFAULT:
            self._set_state(node, LoadState.CONTEXT_STUFFED)
        elif state == LoadState.CONTEXT_STUFFED:
            self._set_state(node, LoadState.UNLOADED)
