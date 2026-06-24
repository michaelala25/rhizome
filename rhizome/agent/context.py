"""Agent context schemas: the langgraph runtime context an agent runs against.

A context is the per-conversation bag of *services and channels* — immutable for a run (per langgraph's
contract), but the objects behind the references are live. Every agent kind declares its own context
schema (a ``BaseAgentContext`` subclass) on its ``AgentDeclaration``; ``AgentRuntime.new`` builds an
instance and the owning ``AgentSession`` holds it for the conversation's lifetime.

Field conventions, honoured by ``AgentRuntime.new``:

- *Framework fields* (``pending``, ``runtime``) are annotated ``... | None`` and filled by the runtime via
  ``dataclasses.replace`` — never injected, never caller-supplied.
- *Service fields* (e.g. ``session_factory``) carry the bare service type as their annotation, so the
  runtime's ``ServiceAccessor`` injects them from the scope.
- Everything else is a caller kwarg passed straight through ``AgentRuntime.new(key, **context_kwargs)``.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rhizome.db import ReadOnlySessionFactoryService, SessionFactoryService
from rhizome.resources_new import ResourceContextStore, ResourceIndexStore

from .app_context import AppContextHookService, LocalAppContextStore
from .engine import PayloadQueue
from .engine_events import EngineEventsChannel
from .topology import TopologyView

if TYPE_CHECKING:
    from .runtime import AgentRuntime


@dataclass
class BaseAgentContext:
    """Framework-managed context fields, filled by ``AgentRuntime.new`` for every session.

    Both are ``None`` on a freshly constructed instance and set by the runtime via ``replace`` — the
    ``| None`` annotations keep them off the injection path, so only the runtime fills them.
    """

    pending: PayloadQueue | None = None
    """Live payload channel shared with the owning ``AgentSession`` (the session posts, the prompt engine
    drains it at each model call). This is the single source of the session's queue — there is no second
    copy to fall out of sync with."""

    runtime: "AgentRuntime | None" = None
    """The ``AgentRuntime``, for tools that spawn or resume conversations with other registered agents:
    ``ctx.runtime.new(key, ...)`` / ``ctx.runtime.get(key, thread_id)``. Imported under ``TYPE_CHECKING``
    only, to avoid an import cycle with the runtime module."""


@dataclass
class RootAgentContext(BaseAgentContext):
    """Context schema for the root conversation agent."""

    local_resources: ResourceContextStore | None = None
    """Node-local context store: the owning conversation's desired local load state."""

    global_resources: ResourceContextStore | None = None
    """Graph-global context store, shared by every conversation on the graph."""

    resource_index: ResourceIndexStore | None = None
    """Graph-global vector index store, one instance shared by every conversation on the graph. The
    prompt engine calls ``consume()`` on it at compile (lazy, incremental ingestion) and emits a
    per-thread "what's queryable" reminder describing its loaded set."""

    app_state: LocalAppContextStore | None = None
    """Node-local store of live app settings (the active mode today). The single source of truth both the
    user (view) and the agent (the ``set_mode`` tool) write through; the prompt engine diffs it against
    ``RootAgentState["mode"]`` at compile and commits the change, narrating it via guides/headers. A live
    channel, never checkpointed — re-supplied per session and carried across a branch by ``copy_from``.
    ``None`` outside the conversation graph that wires it."""

    app_context_hooks: AppContextHookService | None = None
    """The workspace-scoped app-context hook registry (``AppContextHookService``), threaded in by the graph.
    The prompt engine reads its merged effective facts lazily in ``prepare`` and folds them into a single
    tail ``<system-reminder>`` — view-only, never checkpointed, so the facts re-derive per process. The app
    owns the content; the engine owns placement. ``None`` outside a graph that wires it."""

    engine_events: EngineEventsChannel | None = None
    """Per-node engine→app event channel (a pure ``CallbackHost``). The engine emits side-band status the
    token/tool stream doesn't carry — slow context compaction today — and the app subscribes to drive UI
    (e.g. a spinner). Per node, and every event carries the node id, so a compacting branch only signals its
    own view. ``None`` outside a graph that wires it."""

    topology: TopologyView | None = None
    """Shared, graph-global handle to the live topology snapshot — the conversation layer wires the one
    cell in. The prompt engine reads it at compile/prepare to witness the whole current graph (this
    conversation's lineage, branch points, leaf structure). A pull handle, never checkpointed; ``None``
    for sessions not owned by a graph."""

    node_id: int | None = None
    """This conversation's node id within the graph, paired with ``topology`` to orient the snapshot to
    "me" (which segment is the leaf, which messages are inherited). ``None`` outside a graph."""

    session_factory: SessionFactoryService = None
    """DB session factory — the root agent's DB tools open sessions off this, and widgets that write to the
    DB (e.g. the flashcard review widget invoking ``apply_rating``) pull it off the context when constructed
    from an interrupt. Injected by ``AgentRuntime.new`` from the service scope (note the bare service
    annotation); ``None`` where no DB is registered."""

    read_only_session_factory: ReadOnlySessionFactoryService = None
    """Read-only DB session factory — the SQL escape-hatch tool (``execute_sql``) opens sessions off this,
    a dedicated read-only engine (OS-level ``mode=ro`` + a SQLite authorizer) so the agent's raw-SQL reads
    can never write or pull binary blobs. A distinct service from ``session_factory`` so DI hands over the
    read-only instance; ``None`` until that service is registered."""
