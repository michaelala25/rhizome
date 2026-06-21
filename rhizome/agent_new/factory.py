"""Agent factory: the registry of how to build each agent.

A declaration is an agent kind's static manifest:

- ``build`` — a B'-style builder whose service parameters the runtime injects (``ServiceAccessor.inject``)
  and whose option parameters it binds (``Options.inject``): ``Annotated[T, spec]`` for a snapshot value (a
  change rebuilds the agent) and ``Annotated[OptionRef[T], spec]`` for a live handle (read fresh, no
  rebuild). It returns ``(agent, engine)`` — both are option-derived and rebuilt together, and because
  ``build`` is the sole creator of the engine it returns, the engine wired into the agent's
  ``PromptCompilerMiddleware`` and the one the runtime hands to sessions (for post-mortem repair) are the
  same object by construction. The builder's snapshot parameters double as the agent's invalidation set
  (``option_bindings``), so injection sites and invalidation share one source — the signature.
- ``context_schema`` — the ``BaseAgentContext`` subclass the runtime instantiates in ``new`` per session.
- ``state_schema`` / ``response_schema`` — recorded for completeness/introspection; the runtime does not
  consume them (the builder bakes them into ``create_agent``).

The factory holds only declarations; the runtime does the building, caching, and invalidation. ``register``
emits non-fatal warnings for builders that may be sidestepping the snapshot/invalidation contract — the
policy lives here (``_builder_warnings``); the option-annotation facts come from ``options.option_usage``.
"""

import warnings
from dataclasses import dataclass
from typing import Callable, Hashable, Protocol

from langgraph.graph.state import CompiledStateGraph

from rhizome.app.options import option_usage

from .engine import PromptEngine


def _builder_warnings(build: Callable) -> list[str]:
    """Hygiene warnings for a builder's option declarations (empty when clean); none is fatal. The factory
    owns this policy — what shapes are suspicious for an *agent* builder — over the neutral facts that
    ``options.option_usage`` reports."""
    usage = option_usage(build)
    out: list[str] = []
    if usage.lives and not usage.snapshots:
        out.append(
            "declares only live OptionRef option deps and no snapshot OptionSpec deps, so nothing will "
            "invalidate its built agent on an option change — declare any structural option (provider, "
            "model, ...) as a plain Annotated[T, spec] parameter so it triggers a rebuild"
        )
    if usage.wants_service:
        out.append(
            "takes OptionService directly; prefer declaring specific deps as Annotated[T, spec] (snapshot, "
            "rebuilds on change) or Annotated[OptionRef[T], spec] (live, no rebuild) so the invalidation "
            "set stays derivable from the signature"
        )
    return out


@dataclass(frozen=True)
class AgentDeclaration:
    """An agent kind's static manifest — see the module docstring."""

    key: Hashable
    build: Callable[..., tuple[CompiledStateGraph, PromptEngine]]
    context_schema: type
    state_schema: type | None = None
    response_schema: type | None = None


# ==========================================================================================
# Service: AgentFactoryService
#   Shape : protocol + first-party impl (AgentFactory, below)
#   Scope : workspace
#   Notes : the runtime's read-only view of the registry; the full AgentFactory adds ``register``,
#           used only at composition.
# ==========================================================================================


class AgentFactoryService(Protocol):
    """The runtime's read-only view of the registry -- the service it depends on (the full ``AgentFactory``
    adds ``register``, used only at composition). Annotating a dependency with this signals injection."""

    def get(self, key: Hashable) -> AgentDeclaration: ...

    @property
    def declarations(self) -> tuple[AgentDeclaration, ...]: ...


class AgentFactory(AgentFactoryService):
    """The declaration registry. Holds no accessor or runtime -- pure data the runtime consumes."""

    def __init__(self) -> None:
        self._declarations: dict[Hashable, AgentDeclaration] = {}

    def register(
        self,
        key: Hashable,
        *,
        build: Callable[..., tuple[CompiledStateGraph, PromptEngine]],
        context_schema: type,
        state_schema: type | None = None,
        response_schema: type | None = None,
    ) -> None:
        if key in self._declarations:
            raise KeyError(f"Agent already registered: {key}")
        for message in _builder_warnings(build):
            warnings.warn(f"Agent {key!r} builder {message}.", stacklevel=2)
        self._declarations[key] = AgentDeclaration(key, build, context_schema, state_schema, response_schema)

    def get(self, key: Hashable) -> AgentDeclaration:
        try:
            return self._declarations[key]
        except KeyError:
            raise KeyError(f"Unknown agent key: {key}") from None

    @property
    def declarations(self) -> tuple[AgentDeclaration, ...]:
        return tuple(self._declarations.values())
