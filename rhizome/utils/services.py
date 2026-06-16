"""
ServiceAccessor: a scoped dependency-injection container with annotation-driven wiring.

A factory declares its dependencies as annotated parameters; the container reads the annotations,
resolves each one, and calls the factory with them as keyword arguments. The factory is a plain
function of its dependencies -- it never sees the container.

    class AgentRuntime:
        def __init__(
            self,
            *,
            checkpointer: CheckpointerService,           # STRONG   -> resolved instance, cycle-checked
            factory:      Handle[AgentFactoryService],   # WEAK     -> lazy handle, not cycle-checked
            options:      OptionService,                 # STRONG
            accessor:     ServiceAccessor,               # LOCATOR  -> the scoped accessor itself
        ): ...

    services.register_descriptor(AgentRuntimeService, AgentRuntime)   # deps inferred from __init__

Edge kinds, read straight off each parameter's annotation:

    <ServiceType>            STRONG construction edge: eagerly resolved before the factory runs and
                             included in the static cycle check (``validate``).
    Handle[<ServiceType>]    WEAK deferred edge: injected as an unresolved ``Handle`` whose ``.get()``
                             starts a fresh chain. Excluded from cycle detection -- the mechanism that
                             lets two services hold mutual references.
    ServiceAccessor          LOCATOR: injects the scoped accessor itself (durable, chain-free, safe to
                             store) for factory/composition services whose edges are emergent.

Injection rule: a parameter with NO default is a dependency, and its annotation must be a real (not
stringized) type -- so a participating factory's module must avoid ``from __future__ import
annotations``. A parameter WITH a default is configuration and is left alone. An explicit ``requires``
mapping (parameter name -> requirement) overrides inference per parameter: the escape hatch for string
keys, or for injecting a defaulted parameter.

Scope model: a descriptor resolves once per scope that owns it, and the instance is cached there.
``.child()`` scopes fall through to the parent for keys they don't register, and a service always
resolves its own dependencies from the scope it was registered in.
"""

import inspect
from typing import Any, Generic, Hashable, NamedTuple, Optional, TypeVar, Union, get_args, get_origin

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Handle: a deferred reference to a service
# --------------------------------------------------------------------------- #

class Handle(Generic[T]):
    """
    A deferred reference to a service. As a parameter annotation, ``Handle[Key]`` declares a WEAK edge:
    no construction-order constraint, excluded from static cycle detection. ``.get()`` resolves the
    target lazily, starting a fresh resolution chain -- which is what lets two services reference each
    other without a construction cycle.
    """

    __slots__ = ("_key", "_accessor")

    def __init__(self, key: Hashable, accessor: Optional["ServiceAccessor"] = None) -> None:
        self._key = key
        self._accessor = accessor

    @property
    def key(self) -> Hashable:
        return self._key

    def get(self) -> T:
        if self._accessor is None:
            raise RuntimeError(f"Handle({self._key!r}) is unbound; there is no scope to resolve against.")
        return self._accessor.get(self._key)

    def __repr__(self) -> str:
        return f"Handle({self._key!r})"


# --------------------------------------------------------------------------- #
# Dependency model
# --------------------------------------------------------------------------- #

_STRONG, _WEAK, _ACCESSOR = "strong", "weak", "accessor"


class _Dep(NamedTuple):
    kind: str       # _STRONG | _WEAK | _ACCESSOR
    key: Hashable   # the service key (None for _ACCESSOR)


Requirement = Union[Hashable, "Handle"]


def _classify_annotation(ann: Any) -> _Dep:
    """Map a parameter's (real) annotation onto a dependency edge."""
    if ann is Handle:
        raise ServiceError("Handle must be parameterized as Handle[SomeService] to declare a weak edge.")
    if get_origin(ann) is Handle:
        (key,) = get_args(ann)
        return _Dep(_WEAK, key)
    if ann is ServiceAccessor:
        return _Dep(_ACCESSOR, None)
    return _Dep(_STRONG, ann)


def _classify_requirement(req: Requirement) -> _Dep:
    """Map an explicit ``requires`` entry onto a dependency edge."""
    if req is ServiceAccessor:
        return _Dep(_ACCESSOR, None)
    if isinstance(req, Handle):
        return _Dep(_WEAK, req.key)
    if get_origin(req) is Handle:
        (key,) = get_args(req)
        return _Dep(_WEAK, key)
    return _Dep(_STRONG, req)


def _infer_dependencies(factory: Any, requires: Optional[dict[str, Requirement]]) -> dict[str, _Dep]:
    """
    Build the {parameter name -> _Dep} map for a factory: inferred from annotations, then overlaid with
    any explicit ``requires``. Parameters with no default must resolve to an injectable edge; defaulted
    parameters are configuration unless explicitly named in ``requires``.
    """
    signature = inspect.signature(factory)
    explicit = dict(requires or {})
    deps: dict[str, _Dep] = {}

    for pname, param in signature.parameters.items():
        if pname == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        # Explicit requires wins over inference, and may target an otherwise-configuration parameter.
        if pname in explicit:
            deps[pname] = _classify_requirement(explicit.pop(pname))
            continue

        # A defaulted parameter is configuration, not a dependency.
        if param.default is not inspect.Parameter.empty:
            continue

        ann = param.annotation
        if ann is inspect.Parameter.empty:
            raise ServiceError(
                f"{_name(factory)} parameter '{pname}' has no annotation, so its dependency cannot be "
                f"inferred. Annotate it, give it a default to mark it configuration, or list it in requires."
            )
        if isinstance(ann, str):
            raise ServiceError(
                f"{_name(factory)} parameter '{pname}' has a stringized annotation ({ann!r}). DI factories "
                f"must use real annotations -- drop 'from __future__ import annotations' from "
                f"{getattr(factory, '__module__', '<unknown>')!r}, or list the parameter in requires."
            )
        deps[pname] = _classify_annotation(ann)

    if explicit:
        raise ServiceError(
            f"requires for {_name(factory)} names parameter(s) it does not accept: {sorted(map(str, explicit))}."
        )

    return deps


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ServiceError(Exception):
    pass


class ServiceNotFoundError(ServiceError, KeyError):
    pass


class DuplicateRegistrationError(ServiceError, KeyError):
    pass


class CyclicalServiceDependencyError(ServiceError):
    def __init__(self, chain: tuple) -> None:
        self.chain = tuple(chain)
        super().__init__("Dependency cycle detected: " + " -> ".join(_name(k) for k in self.chain))


# --------------------------------------------------------------------------- #
# Descriptor
# --------------------------------------------------------------------------- #

class ServiceDescriptor:
    """A factory together with its resolved dependency map, computed once at registration."""

    __slots__ = ("factory", "deps", "strong_deps", "weak_deps", "wants_accessor")

    def __init__(self, factory: Any, requires: Optional[dict[str, Requirement]] = None) -> None:
        self.factory = factory
        self.deps = _infer_dependencies(factory, requires)
        self.strong_deps = tuple(d.key for d in self.deps.values() if d.kind is _STRONG)
        self.weak_deps = tuple(d.key for d in self.deps.values() if d.kind is _WEAK)
        self.wants_accessor = any(d.kind is _ACCESSOR for d in self.deps.values())

    def __repr__(self) -> str:
        return (
            f"ServiceDescriptor({_name(self.factory)}, "
            f"strong={tuple(map(_name, self.strong_deps))}, weak={tuple(map(_name, self.weak_deps))})"
        )


# --------------------------------------------------------------------------- #
# The container
# --------------------------------------------------------------------------- #

class ServiceAccessor:
    """
    A scoped DI container. Create children with ``.child()``; resolution falls through to the parent
    when a key is not registered locally. A descriptor is invoked once per scope that owns it, and the
    instance is cached in that scope.
    """

    def __init__(self, parent: Optional["ServiceAccessor"] = None) -> None:
        self._parent = parent
        self._descriptors: dict[Hashable, ServiceDescriptor] = {}
        self._instances: dict[Hashable, Any] = {}

    # ----- registration ---------------------------------------------------- #

    def register(self, key: Hashable, value: Any) -> "ServiceAccessor":
        """Register a ready-made instance at this scope."""
        self._guard_unregistered(key)
        self._instances[key] = value
        return self

    def register_descriptor(
        self,
        key: Hashable,
        descriptor: Optional[Any] = None,
        *,
        requires: Optional[dict[str, Requirement]] = None,
    ) -> "ServiceAccessor":
        """
        Register a lazily-constructed service.

        key        : the service key (typically the protocol/type the value satisfies).
        descriptor : the factory -- a callable (often a class) whose annotated parameters declare its
                     dependencies. If omitted, ``key`` itself is used as the factory.
        requires   : optional {parameter name -> requirement} overrides for parameters that cannot be
                     inferred (string keys) or are otherwise configuration. A requirement is a service
                     key (STRONG), ``Handle(key)`` (WEAK), or ``ServiceAccessor`` (locator).
        """
        self._guard_unregistered(key)
        factory = descriptor if descriptor is not None else key
        if not callable(factory):
            raise TypeError("register_descriptor needs a callable factory (pass one, or use a callable key).")
        self._descriptors[key] = ServiceDescriptor(factory, requires)
        return self

    def _guard_unregistered(self, key: Hashable) -> None:
        if key in self._descriptors or key in self._instances:
            raise DuplicateRegistrationError(f"{_name(key)} is already registered in this scope.")

    # ----- resolution ------------------------------------------------------ #

    def get(self, key: Hashable) -> Any:
        """Resolve a service, starting a fresh resolution chain."""
        return self._get(key, ())

    def _get(self, key: Hashable, _resolving: tuple) -> Any:
        if key in self._instances:
            return self._instances[key]

        descriptor = self._descriptors.get(key)
        if descriptor is None:
            # Fall through to the parent, carrying the live chain so cross-scope construction still
            # detects cycles.
            if self._parent is None:
                raise ServiceNotFoundError(_name(key))
            return self._parent._get(key, _resolving)

        if key in _resolving:
            raise CyclicalServiceDependencyError((*_resolving, key))

        chain = (*_resolving, key)
        kwargs: dict[str, Any] = {}
        for pname, dep in descriptor.deps.items():
            if dep.kind is _STRONG:
                kwargs[pname] = self._get(dep.key, chain)
            elif dep.kind is _WEAK:
                kwargs[pname] = Handle(dep.key, self)
            else:  # _ACCESSOR
                kwargs[pname] = self

        instance = descriptor.factory(**kwargs)
        self._instances[key] = instance
        return instance

    def handle(self, key: Hashable) -> Handle:
        """A bound handle for deferred resolution from this scope."""
        return Handle(key, self)

    # ----- scoping --------------------------------------------------------- #

    def child(self) -> "ServiceAccessor":
        return ServiceAccessor(parent=self)

    # ----- static validation ---------------------------------------------- #

    def validate(self, *, include_ancestors: bool = True) -> None:
        """
        Statically check the declared dependency graph for construction cycles (cycles of STRONG edges).
        Weak (``Handle``) edges and locator (``ServiceAccessor``) nodes are excluded by design. Run this
        at end-of-registration to fail fast, before the first resolution.
        """
        descriptors = self._collect_descriptors(include_ancestors)

        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[Hashable, int] = {}
        stack: list[Hashable] = []

        def visit(node: Hashable) -> None:
            color[node] = GREY
            stack.append(node)
            desc = descriptors.get(node)
            if desc is not None:
                for dep in desc.strong_deps:
                    # An edge to a key with no descriptor (a register()'d instance, or a key only a
                    # parent we didn't collect provides) cannot start a declaration cycle: skip it.
                    if dep not in descriptors:
                        continue
                    c = color.get(dep, WHITE)
                    if c == GREY:
                        i = stack.index(dep)
                        raise CyclicalServiceDependencyError((*stack[i:], dep))
                    if c == WHITE:
                        visit(dep)
            stack.pop()
            color[node] = BLACK

        for node in descriptors:
            if color.get(node, WHITE) == WHITE:
                visit(node)

    def _collect_descriptors(self, include_ancestors: bool) -> dict[Hashable, ServiceDescriptor]:
        merged: dict[Hashable, ServiceDescriptor] = {}
        chain: list[ServiceAccessor] = []
        node: Optional[ServiceAccessor] = self
        while node is not None:
            chain.append(node)
            if not include_ancestors:
                break
            node = node._parent
        # Nearer scopes shadow farther ones for the same key.
        for scope in reversed(chain):
            merged.update(scope._descriptors)
        return merged

    def __repr__(self) -> str:
        depth = 0
        node = self._parent
        while node is not None:
            depth += 1
            node = node._parent
        return (
            f"<ServiceAccessor depth={depth} "
            f"descriptors={list(map(_name, self._descriptors))} "
            f"instances={list(map(_name, self._instances))}>"
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _name(key: Any) -> str:
    if isinstance(key, type):
        return key.__name__
    return key if isinstance(key, str) else repr(key)
