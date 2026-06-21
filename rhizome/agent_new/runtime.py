"""Agent runtime: build/cache/invalidate agents, and own the live conversation sessions.

Workspace-scoped (one per session). Two responsibilities:

- *Builds.* Per agent key, the runtime builds and caches ``(agent, engine)`` — currying the builder's
  service parameters (``ServiceAccessor.inject``) and option values (``Options.inject``) — and drops the
  cache entry when any bound option changes. The bound (invalidating) options are the builder's SNAPSHOT
  ``Annotated[T, spec]`` parameters (``option_bindings``); live ``OptionRef`` parameters read fresh and
  don't invalidate. A session reaches the cached agent/engine through the ``_get_agent`` /
  ``_get_prompt_engine`` friend accessors.
- *Sessions.* The runtime owns every ``AgentSession``. ``new`` mints a thread id, builds the declared
  context (injecting service fields, filling the framework ``pending``/``runtime`` fields), constructs
  the session, and stores it under ``(key, thread_id)``. ``get`` retrieves it. A session resolves the
  current template on every ``acquire``, so an option-driven rebuild is invisible to an in-flight
  conversation; its context and thread id ride through untouched.

Build it through the service container -- its dependencies are injected by annotation.
"""

import functools
import uuid
from dataclasses import replace
from typing import Any, Hashable

from langgraph.graph.state import CompiledStateGraph

from rhizome.app.options import option_bindings, OptionService
from rhizome.utils.services import ServiceAccessor

from .engine import PayloadQueue, PromptEngine
from .factory import AgentFactoryService
from .session import AgentSession


def new_workspace_id() -> str:
    """A short, unique-enough id prefixed onto a workspace's thread ids -- namespacing in the shared
    checkpointer (grouping, bulk ops, readable logs); the uuid suffix handles actual uniqueness."""
    return uuid.uuid4().hex[:8]


class AgentRuntime:
    """Workspace-scoped agent runtime. See the module docstring for the model."""

    def __init__(self, *, accessor: ServiceAccessor, factory: AgentFactoryService, options: OptionService) -> None:
        self._accessor = accessor
        self._factory = factory
        self._options = options
        self._workspace_id = new_workspace_id()
        self._built: dict[Hashable, tuple[CompiledStateGraph, PromptEngine]] = {}   # cached (agent, engine)
        self._sessions: dict[tuple[Hashable, str], AgentSession] = {}
        self._bound_keys: set[Hashable] = set()                       # keys whose invalidation is wired
        self._binding_handlers: list[Any] = []                        # strong refs; subscribers held weakly

    # ----- invalidation ---------------------------------------------------- #

    def _ensure_bindings(self, key: Hashable) -> None:
        # Subscribe this key's invalidation to its bound options, once, on first build. Done lazily rather
        # than at construction so it is independent of agent-registration order (agents are often
        # registered after the runtime is built) — and there is nothing to invalidate until the key's
        # graph is first cached anyway. The bound options ARE the builder's option-annotated parameters
        # (``option_bindings``). Strong refs to the handlers are kept because ``CallbackHost`` holds
        # subscribers weakly — a bare handler would be collected and invalidation would silently stop.
        if key in self._bound_keys:
            return
        self._bound_keys.add(key)
        for spec in option_bindings(self._factory.get(key).build):
            handler = functools.partial(self._invalidate, key)
            self._binding_handlers.append(handler)
            self._options.subscribe_on_changed(spec, handler)

    def _invalidate(self, key: Hashable, *_: Any) -> None:
        # Drop the cached (agent, engine); rebuilt lazily on the next build. Sessions are untouched — each
        # keeps its thread id and context and picks up the rebuild on its next ``acquire``. Extra args
        # absorb the ``(prev, new)`` an option-change subscription passes.
        self._built.pop(key, None)

    # ----- build cache & friend getters ------------------------------------ #

    def _ensure_built(self, key: Hashable) -> tuple[CompiledStateGraph, PromptEngine]:
        """Build and cache ``(agent, engine)`` for ``key`` — on first use, and after an invalidation.

        Curries services (inner) then option values (outer) into the builder; each pass leaves the other's
        parameters open, so the composition resolves every parameter and the final call returns the pair.
        Agent and engine come from one ``build`` call and one cache entry, so they cannot drift."""
        if key not in self._built:
            self._ensure_bindings(key)
            self._built[key] = self._options.inject(self._accessor.inject(self._factory.get(key).build))()
        return self._built[key]

    # ``_get_agent`` / ``_get_prompt_engine`` are friend accessors for ``AgentSession`` (its ``acquire`` and
    # post-mortem repair). Public consumers go through ``new`` / ``get``; these hand a live session the
    # option-derived internals it needs without exposing the build cache.

    def _get_agent(self, key: Hashable) -> CompiledStateGraph:
        return self._ensure_built(key)[0]

    def _get_prompt_engine(self, key: Hashable) -> PromptEngine:
        return self._ensure_built(key)[1]

    # ----- sessions -------------------------------------------------------- #

    def new(self, key: Hashable, **context_kwargs: Any) -> AgentSession:
        """Create a fresh session: mint a thread id, build the declared context (service fields injected
        from the scope, the framework ``pending`` queue + ``runtime`` backref filled here), construct the
        session, and store it under ``(key, thread_id)``. ``context_kwargs`` are the context schema's
        remaining (non-framework, non-service) fields — e.g. resource stores, hooks."""
        decl = self._factory.get(key)
        thread_id = f"{self._workspace_id}:{uuid.uuid4().hex}"
        built = self._accessor.inject(decl.context_schema)(**context_kwargs)
        context = replace(built, pending=PayloadQueue(), runtime=self)
        session = AgentSession(self, key, thread_id, context)
        self._sessions[(key, thread_id)] = session
        return session

    def get(self, key: Hashable, thread_id: str) -> AgentSession:
        """Retrieve a session created earlier by ``new`` — e.g. to resume a conversation whose thread id a
        tool emitted. Raises ``KeyError`` if this runtime holds no such session."""
        try:
            return self._sessions[(key, thread_id)]
        except KeyError:
            raise KeyError(f"no session for ({key!r}, {thread_id!r})") from None


# ==========================================================================================
# Service: AgentRuntimeService
#   Shape : alias -- the contract is the whole AgentRuntime (builders need it un-narrowed)
#   Scope : workspace
# ==========================================================================================
# Aliased so the dependency site reads as an injected service (``runtime: AgentRuntimeService``).
AgentRuntimeService = AgentRuntime
