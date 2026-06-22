"""WorkspaceModel — the orchestrator VM for one workspace (a chat tab's worth of UI).

The workspace is the composition root for the conversation stack: it opens the workspace-scoped service
container, declares the per-workspace ``AgentRuntime`` there, and owns the panel VMs surfaced within it.
``ChatAreaModel`` is the always-present center panel; the resource viewer and an auxiliary right panel are
owned here too (stubbed until their VMs are ported). The status bar is not a panel — it is a fixed element
of the chat area, owned by ``ChatAreaModel``. Everything else is a panel for now — the panel/chrome
distinction is deferred.

Two-phase construction
----------------------
``__init__`` only wires the service scope and registers panel descriptors — cheap and side-effect-light.
``bootstrap`` (called by the view at mount) surfaces the initial panel set, building the panels then: the
chat area mints a runtime session, so deferring it to mount keeps construction free of that weight and
gives the view a seam to thread mount-time inputs (app-level flags) the service scope can't supply.

Service scoping
---------------
A ``"workspace"`` child scope shadows the accessor it's handed. The ``AgentRuntimeService`` descriptor is
declared *here* so the runtime builds at this scope — one runtime per workspace (``AgentRuntime`` is
workspace-scoped by contract). App-global services fall through to root: the agent factory (carrying the
agents, registered once at app composition — agent registration is NOT a per-workspace concern), the
shared checkpointer, the session factory. A per-workspace ``Options`` override shadows the root so
``/options`` edits stay local.

Panel custody belongs to ``OrchestratorModel``: each panel VM builds lazily on first request and lives
for the workspace, so its state survives the view being rebuilt or the panel being hidden.
"""

from __future__ import annotations

from rhizome.agent.runtime import AgentRuntime, AgentRuntimeService
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.commands import CommandRegistry, CommandRegistryService
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.app.orchestrator import OrchestratorModel
from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.db import SessionFactoryService
from rhizome.resources_new import ResourceContextStore, ResourceIndexStore, ResourceTree, ResourceVectorStore
from rhizome.utils.services import ServiceAccessor


class WorkspaceModel(OrchestratorModel):

    def __init__(self, services: ServiceAccessor) -> None:
        super().__init__()

        # Workspace-scoped service container. App-global services fall through to root; the pieces below
        # shadow at this scope.
        self._services = services.child("workspace")

        # Per-workspace Options override: a Session node parented to the app's Root options, so ``/options``
        # edits stay local while inherited Root changes still forward in. Options are required — the
        # workspace is a composition root, so its environment is a contract, not something to probe for.
        root_options = services.get(OptionService).at_scope(OptionScope.Root)
        self._options = Options(OptionScope.Session, parent=root_options)
        self._services.register(OptionService, self._options)

        # Declare the runtime at THIS scope so it builds here — one AgentRuntime per workspace. Its build
        # deps (factory, checkpointer, options) inject from the scope: factory + checkpointer resolve from
        # root, options from the shadow above.
        self._services.register_descriptor(AgentRuntimeService, AgentRuntime)

        # Workspace command registry: parented to the app-global registry (resolved from the parent scope),
        # registered as the workspace-scope CommandRegistryService, and injected into the chat area, which
        # registers its conversation commands on it. App / tab commands live in the global registry this
        # one inherits from; workspace-level commands (/resources, /rename) register here as they port.
        self._commands = CommandRegistry(parent=services.try_get(CommandRegistryService))
        self._services.register(CommandRegistryService, self._commands)

        # Resource layer: one ``ResourceTree`` per workspace, shared by the conversation graph's stores
        # and the loader panel. Construction is side-effect-free — no DB is touched until a refresh, which
        # the loader's ``load`` drives. The global context store caches built blocks (one instance backs
        # every branch); per-node local stores are minted uncached by the factory.
        self._session_factory = self._services.get(SessionFactoryService)
        self._resource_tree = ResourceTree(self._session_factory)
        self._resource_context = ResourceContextStore(self._resource_tree, cache=True)
        self._resource_index = ResourceIndexStore(
            self._resource_tree, index=ResourceVectorStore(self._session_factory)
        )
        self._local_resources_factory = lambda: ResourceContextStore(self._resource_tree)

        # Panel descriptors. Registration is cheap (no build); ``bootstrap`` surfaces the initial set. An
        # auxiliary right panel registers here too once its VM lands. (The status bar is not a panel — it
        # lives inside the chat area, owned by ``ChatAreaModel``.)
        self._register_view_model(ChatAreaModel, self._build_chat_area)
        self._register_view_model(ResourceLoaderModel, self._build_resource_loader)

    def bootstrap(self) -> None:
        """Surface the workspace's initial panels — the second construction phase, called by the view at
        mount. Building the chat area mints a runtime session, so this is deliberately not in ``__init__``;
        it is also the seam for mount-time inputs the service scope can't supply (app-level flags, etc.)."""
        self.request_mount(self._get_view_model(ChatAreaModel))
        self.request_mount(self._get_view_model(ResourceLoaderModel))

    # ------------------------------------------------------------------
    # Typed panel accessors
    # ------------------------------------------------------------------

    @property
    def chat_area(self) -> ChatAreaModel:
        """The conversation panel — one instance, custody held for the workspace's life."""
        return self._get_view_model(ChatAreaModel)

    @property
    def resource_loader(self) -> ResourceLoaderModel:
        """The resource-loader panel — one instance, custody held for the workspace's life."""
        return self._get_view_model(ResourceLoaderModel)

    # ------------------------------------------------------------------
    # Panel descriptors
    # ------------------------------------------------------------------

    def _build_chat_area(self, _orch: OrchestratorModel) -> ChatAreaModel:
        """Build the conversation panel over this workspace's runtime, wired to the shared resource
        stores so loads made in the loader panel reach the agent on its next turn."""
        runtime = self._services.get(AgentRuntimeService)
        return ChatAreaModel(
            runtime,
            command_registry=self._commands,
            resource_context=self._resource_context,
            resource_index=self._resource_index,
            local_resources_factory=self._local_resources_factory,
            options=self._options,
        )

    def _build_resource_loader(self, _orch: OrchestratorModel) -> ResourceLoaderModel:
        """Build the loader panel over the shared resource stores. Its local-context axis tracks the
        conversation's current leaf — seeded from the chat area's cursor here, re-pointed on every move."""
        chat_area = self.chat_area   # lazily builds the chat area: the loader's local store comes from it
        loader = ResourceLoaderModel(
            self._session_factory,
            self._resource_tree,
            index=self._resource_index,
            global_context=self._resource_context,
            local_context=chat_area.cursor.node.resources,
        )
        chat_area.subscribe(chat_area.Callbacks.OnCursorMoved, self._on_chat_cursor_moved)
        return loader

    def _on_chat_cursor_moved(self, cursor) -> None:
        """Re-point the loader's local-context channel at the new conversation leaf's node-local store."""
        self.resource_loader.set_local_context_store(cursor.node.resources)
