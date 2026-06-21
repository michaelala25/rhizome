"""Option definitions, persistence, validation, and change events for the TUI.

Provides a hierarchical options system with scoped inheritance (Root → Session),
validation, JSONC persistence, and synchronous change events via ``CallbackHost``.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, NamedTuple, Protocol

from rhizome.config import get_options_path
from rhizome.logs import get_logger
from rhizome.utils.callbacks import CallbackHost


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OptionScope(IntEnum):
    """Scope at which an option can be set."""

    Root = 0
    Session = 1


# ---------------------------------------------------------------------------
# OptionSpec hierarchy
# ---------------------------------------------------------------------------


class OptionSpec:
    """Base option specification: name, scope, default, help text.

    ``immediate=True`` opts the spec out of editor staging — ``OptionsEditorModel`` writes such
    values straight to the live ``Options`` target rather than to its scratch clone. Reserve
    for changes the user has to see take effect live (theme, display-only fields). Off by
    default; flag explicitly.
    """

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        default: Any,
        help: str,
        *,
        immediate: bool = False,
    ) -> None:
        self.name = name
        self.resolved_name: str = name  # overwritten by metaclass
        self.scope = scope
        self.default = default
        self.help = help
        self.immediate = immediate

    def validate(self, value: Any) -> Any:
        """Validate and return the (possibly coerced) value, or raise ValueError."""
        return value

    def from_string(self, raw: str) -> Any:
        """Parse a string into the option's native type."""
        return raw.strip()

    def jsonc_comment(self) -> str:
        """Return JSONC comment text (without leading ``//``)."""
        return self.help


class ChoicesOptionSpec(OptionSpec):
    """Option constrained to a fixed set of choices."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        default: Any,
        help: str,
        choices: list[Any],
        *,
        immediate: bool = False,
    ) -> None:
        super().__init__(name, scope, default, help, immediate=immediate)
        self.choices = choices

    def validate(self, value: Any) -> Any:
        if value not in self.choices:
            raise ValueError(f"Must be one of: {', '.join(str(c) for c in self.choices)}")
        return value

    def from_string(self, raw: str) -> Any:
        return self.validate(raw.strip())

    def jsonc_comment(self) -> str:
        return f"{self.help}\n// Choices: {', '.join(str(c) for c in self.choices)}"


class ConditionalChoicesOptionSpec(OptionSpec):
    """Option whose available choices depend on the current value of another option."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        help: str,
        condition: OptionSpec,
        choices: dict[Any, list[Any]],
        defaults: dict[Any, Any],
        *,
        immediate: bool = False,
    ) -> None:
        self.condition = condition
        self._choices = choices
        self.defaults = defaults
        default = defaults[condition.default]
        super().__init__(name, scope, default, help, immediate=immediate)

    def validate(self, value: Any, *, condition_value: Any = None) -> Any:
        """Validate *value* against the choices for *condition_value*.

        When *condition_value* is ``None`` (e.g. during JSONC load), accept any
        value present in *any* branch.
        """
        if condition_value is None:
            all_values = [v for branch in self._choices.values() for v in branch]
            if value not in all_values:
                raise ValueError(
                    f"Must be one of: {', '.join(str(c) for c in all_values)}"
                )
        else:
            valid = self._choices.get(condition_value, [])
            if value not in valid:
                raise ValueError(
                    f"Must be one of: {', '.join(str(c) for c in valid)}"
                )
        return value

    def choices_for(self, condition_value: Any) -> list[Any]:
        """Return the choices list for a given condition value."""
        return self._choices.get(condition_value, [])

    def default_for(self, condition_value: Any) -> Any:
        """Return the default for a given condition value."""
        return self.defaults[condition_value]

    def from_string(self, raw: str) -> Any:
        return raw.strip()

    def jsonc_comment(self) -> str:
        lines = [self.help]
        for cond, choices in self._choices.items():
            lines.append(f"// {cond}: {', '.join(str(c) for c in choices)}")
        return "\n".join(lines)


class IntRangeOptionSpec(OptionSpec):
    """Option constrained to an integer range."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        default: int,
        help: str,
        min: int,
        max: int,
        *,
        immediate: bool = False,
    ) -> None:
        super().__init__(name, scope, default, help, immediate=immediate)
        self.min = min
        self.max = max

    def validate(self, value: Any) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Expected integer, got {value!r}")
        if v < self.min or v > self.max:
            raise ValueError(f"Must be between {self.min} and {self.max}")
        return v

    def from_string(self, raw: str) -> int:
        return self.validate(raw.strip())

    def jsonc_comment(self) -> str:
        return f"{self.help} ({self.min}-{self.max})"


class FloatRangeOptionSpec(OptionSpec):
    """Option constrained to a float range."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        default: float,
        help: str,
        min: float,
        max: float,
        step: float | None = None,
        *,
        immediate: bool = False,
    ) -> None:
        super().__init__(name, scope, default, help, immediate=immediate)
        self.min = min
        self.max = max
        self.step = step

    def validate(self, value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Expected number, got {value!r}")
        if v < self.min or v > self.max:
            raise ValueError(f"Must be between {self.min} and {self.max}")
        return v

    def from_string(self, raw: str) -> float:
        return self.validate(raw.strip())

    def jsonc_comment(self) -> str:
        return f"{self.help} ({self.min}-{self.max})"


class ConditionalIntRangeOptionSpec(OptionSpec):
    """Integer range option whose bounds depend on the current value of another option."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        help: str,
        condition: OptionSpec,
        ranges: dict[Any, tuple[int, int]],
        defaults: dict[Any, int],
        *,
        immediate: bool = False,
    ) -> None:
        self.condition = condition
        self._ranges = ranges
        self.defaults = defaults
        default = defaults[condition.default]
        super().__init__(name, scope, default, help, immediate=immediate)

    def validate(self, value: Any, *, condition_value: Any = None) -> int:
        """Validate *value* against the range for *condition_value*.

        When *condition_value* is ``None`` (e.g. during JSONC load), accept any
        value within the union of all branches' ranges.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Expected integer, got {value!r}")

        if condition_value is None:
            all_min = min(r[0] for r in self._ranges.values())
            all_max = max(r[1] for r in self._ranges.values())
            if v < all_min or v > all_max:
                raise ValueError(f"Must be between {all_min} and {all_max}")
        else:
            rng = self._ranges.get(condition_value)
            if rng is None:
                raise ValueError(f"Unknown condition value: {condition_value!r}")
            if v < rng[0] or v > rng[1]:
                raise ValueError(f"Must be between {rng[0]} and {rng[1]}")
        return v

    def range_for(self, condition_value: Any) -> tuple[int, int]:
        """Return the ``(min, max)`` range for a given condition value."""
        return self._ranges[condition_value]

    def default_for(self, condition_value: Any) -> int:
        """Return the default for a given condition value."""
        return self.defaults[condition_value]

    def from_string(self, raw: str) -> int:
        return self.validate(raw.strip())

    def jsonc_comment(self) -> str:
        lines = [self.help]
        for cond, (lo, hi) in self._ranges.items():
            lines.append(f"// {cond}: {lo}-{hi}")
        return "\n".join(lines)


class ToggleOptionSpec(ChoicesOptionSpec):
    """Boolean-like option with ``"enabled"`` / ``"disabled"`` choices."""

    def __init__(
        self,
        name: str,
        scope: OptionScope,
        default: str,
        help: str,
        *,
        immediate: bool = False,
    ) -> None:
        super().__init__(
            name, scope, default, help, choices=["enabled", "disabled"], immediate=immediate,
        )


# ---------------------------------------------------------------------------
# OptionNamespace
# ---------------------------------------------------------------------------


class OptionNamespace:
    """Marker base for nested option groups.

    Subclass this inside an ``Options`` class and set ``name = "..."``
    to create a dotted namespace (e.g. ``agent.model``).
    """

    name: str
    resolved_name: str = ""
    description: str = ""


@dataclass
class OptionNamespaceNode:
    """A node in the hierarchical spec tree."""

    namespace: type[OptionNamespace]
    options: list[OptionSpec] = field(default_factory=list)
    children: list[OptionNamespaceNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Metaclass
# ---------------------------------------------------------------------------


def _collect_specs(
    namespace: type,
    prefix: str,
    target: list[OptionSpec],
) -> None:
    """Recursively walk *namespace* and wire up resolved names."""
    for attr_name in list(vars(namespace)):
        obj = getattr(namespace, attr_name)
        if isinstance(obj, OptionSpec):
            obj.resolved_name = f"{prefix}.{obj.name}" if prefix else obj.name
            target.append(obj)
        elif isinstance(obj, type) and issubclass(obj, OptionNamespace) and obj is not OptionNamespace:
            ns_name = getattr(obj, "name", attr_name.lower())
            obj.resolved_name = f"{prefix}.{ns_name}" if prefix else ns_name
            _collect_specs(obj, obj.resolved_name, target)


def _build_spec_tree(
    cls: type,
) -> tuple[list[OptionSpec], list[OptionNamespaceNode]]:
    """Build a hierarchical spec tree from the class.

    Returns ``(top_level_options, namespace_nodes)`` where top-level options
    are specs defined directly on *cls* (not inside a namespace).
    """
    top_level: list[OptionSpec] = []
    nodes: list[OptionNamespaceNode] = []

    for attr_name in list(vars(cls)):
        obj = getattr(cls, attr_name)
        if isinstance(obj, OptionSpec):
            top_level.append(obj)
        elif isinstance(obj, type) and issubclass(obj, OptionNamespace) and obj is not OptionNamespace:
            nodes.append(_build_ns_node(obj))

    return top_level, nodes


def _build_ns_node(ns: type[OptionNamespace]) -> OptionNamespaceNode:
    """Recursively build an ``OptionNamespaceNode`` for *ns*."""
    node = OptionNamespaceNode(namespace=ns)
    for attr_name in list(vars(ns)):
        obj = getattr(ns, attr_name)
        if isinstance(obj, OptionSpec):
            node.options.append(obj)
        elif isinstance(obj, type) and issubclass(obj, OptionNamespace) and obj is not OptionNamespace:
            node.children.append(_build_ns_node(obj))
    return node


class OptionsMeta(type):
    """Metaclass that walks class attrs to build a flat spec registry and tree."""

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> type:
        cls = super().__new__(mcs, name, bases, namespace)
        specs: list[OptionSpec] = []
        _collect_specs(cls, "", specs)
        cls._all_specs = specs  # type: ignore[attr-defined]
        cls._spec_tree = _build_spec_tree(cls)  # type: ignore[attr-defined]
        return cls


# ---------------------------------------------------------------------------
# Annotation-driven option injection
# ---------------------------------------------------------------------------
# The option-value analogue of ``ServiceAccessor.inject``. A builder declares option dependencies as
# annotated parameters, in one of two flavors that differ by the requested type:
#
#   provider: Annotated[str, Options.Agent.Provider]
#       SNAPSHOT — bound to the current value; a change rebuilds the agent (it joins the invalidation set).
#   ttl: Annotated[OptionRef[str], Options.Agent.Anthropic.PromptCacheTTL]
#       LIVE — bound to a handle read fresh on each ``.get()``; a change does NOT rebuild.
#
# Snapshot is the safe default (a change always takes effect, via rebuild); live is a deliberate opt-in for
# behavioral options a long-lived object reads on demand (e.g. an engine stamping a cache TTL). Both are
# DECLARED — a builder never receives a general ``OptionService``, so every option dependency is visible in
# the signature and the requested type says whether it invalidates. ``Options.inject`` composes with
# ``ServiceAccessor.inject`` (``options.inject(services.inject(build))()``): each binds only what it
# recognizes and leaves the rest open.


class OptionRef[T]:
    """A live, single-spec view of an option value: ``.get()`` reads the current value on each call.

    The live counterpart to a snapshot-injected value — an object holding an ``OptionRef`` reacts to option
    changes without being rebuilt. Declared as ``Annotated[OptionRef[T], spec]`` on a builder parameter;
    the runtime binds one and, unlike a snapshot, leaves its spec OUT of the agent's invalidation set.
    Mirrors ``services.Handle``: a typed handle that opts a dependency out of one piece of automatic
    machinery (there, cycle detection; here, invalidation)."""

    __slots__ = ("_options", "_spec")

    def __init__(self, options: "OptionService", spec: OptionSpec) -> None:
        self._options = options
        self._spec = spec

    def get(self) -> T:
        return self._options.get(self._spec)

    @property
    def spec(self) -> OptionSpec:
        return self._spec

    def __repr__(self) -> str:
        return f"OptionRef({self._spec.resolved_name!r})"


def _option_binding(annotation: Any) -> tuple[OptionSpec, bool] | None:
    """Classify a parameter annotation as an option dependency.

    Returns ``(spec, live)`` — ``live`` is ``True`` for ``Annotated[OptionRef[T], spec]`` (a live handle,
    excluded from invalidation) and ``False`` for ``Annotated[T, spec]`` (a snapshot value, triggers
    invalidation). ``None`` for any non-option annotation."""
    if get_origin(annotation) is not Annotated:
        return None
    args = get_args(annotation)
    spec = next((m for m in args[1:] if isinstance(m, OptionSpec)), None)
    if spec is None:
        return None
    target = args[0]
    live = target is OptionRef or get_origin(target) is OptionRef
    return spec, live


class OptionUsage(NamedTuple):
    """A builder's option dependencies, classified by kind — neutral facts, no policy.

    ``snapshots`` are the invalidation triggers (``Annotated[T, spec]``); ``lives`` are live ``OptionRef``
    reads that never invalidate (``Annotated[OptionRef[T], spec]``); ``wants_service`` is ``True`` when the
    builder takes a general ``OptionService`` parameter (the un-narrowed accessor). Consumers decide what to
    do with these — the runtime subscribes to ``snapshots``; the agent factory warns on suspicious shapes."""

    snapshots: tuple[OptionSpec, ...]
    lives: tuple[OptionSpec, ...]
    wants_service: bool


def option_usage(fn: Callable) -> OptionUsage:
    """Classify ``fn``'s option dependencies (see ``OptionUsage``) by pure signature inspection."""
    snapshots: list[OptionSpec] = []
    lives: list[OptionSpec] = []
    wants_service = False
    for name, param in inspect.signature(fn).parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.annotation is OptionService:
            wants_service = True
            continue
        binding = _option_binding(param.annotation)
        if binding is not None:
            (lives if binding[1] else snapshots).append(binding[0])
    return OptionUsage(tuple(dict.fromkeys(snapshots)), tuple(dict.fromkeys(lives)), wants_service)


def option_bindings(fn: Callable) -> tuple[OptionSpec, ...]:
    """The SNAPSHOT option specs ``fn`` depends on — its invalidation triggers. The runtime subscribes to
    each so a change rebuilds the agent; live ``OptionRef`` parameters are excluded (they read fresh and
    never need a rebuild). Convenience for ``option_usage(fn).snapshots``."""
    return option_usage(fn).snapshots


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class OptionService(Protocol):
    """The consumer-facing slice of ``Options`` -- read a spec's value, subscribe to its changes, curry
    option values into an injectable callable.

    Consumers (e.g. the agent runtime) depend on this protocol rather than the concrete ``Options`` so
    the dependency reads as an injected service and stays off the full options machinery; ``Options``
    satisfies it structurally."""

    def get(self, spec: OptionSpec) -> Any: ...
    def subscribe_on_changed(self, spec: OptionSpec, handler: Callable[[Any, Any], None]) -> None: ...
    def inject(self, fn: Callable) -> Callable: ...


class Options(CallbackHost, metaclass=OptionsMeta):
    """Hierarchical, scoped option store with change events and JSONC persistence.

    **Class-level**: ``OptionSpec`` and ``OptionNamespace`` members define the
    schema (wired by ``OptionsMeta``).

    **Instance-level**: holds ``_values`` and parent/child links, and emits two
    ``CallbackHost`` events:

    - ``OnChanged(scope, key, prev, new)`` — fired per individual change. ``scope`` is the
      ``OptionScope`` at which the change *originated*; ``key`` is the ``OptionSpec``. A change at a
      parent scope is forwarded into each child scope that doesn't locally override ``key`` (with
      ``scope`` left unchanged), so a child's observers and conditional cascades react to inherited
      changes exactly as they would to local ones.
    - ``OnBatchUpdated(prev, new)`` — fired by ``post_update`` once a batch of changes settles, with
      ``prev``/``new`` full ``{resolved_name: value}`` snapshots of effective values.

    ``subscribe_on_changed(spec, handler)`` is a convenience over ``OnChanged`` for watching one
    spec: ``handler(prev, new)`` fires only for that spec (inherited changes included).
    """

    class Callbacks:
        OnChanged = "OnChanged"            # (scope: OptionScope, key: OptionSpec, prev, new)
        OnBatchUpdated = "OnBatchUpdated"  # (prev: dict[str, Any], new: dict[str, Any])

    # ---- Schema (class-level) ----

    Theme = ChoicesOptionSpec(
        name="theme",
        scope=OptionScope.Root,
        default="textual-dark",
        help="Textual color theme",
        choices=[
            "textual-dark",
            "textual-light",
            "nord",
            "gruvbox",
            "catppuccin-mocha",
            "textual-ansi",
            "dracula",
            "tokyo-night",
            "monokai",
            "flexoki",
            "catppuccin-latte",
            "solarized-light",
            "solarized-dark",
            "rose-pine",
            "rose-pine-moon",
            "rose-pine-dawn",
            "atom-one-dark",
            "atom-one-light",
        ],
    )

    UserName = OptionSpec(
        name="user_name",
        scope=OptionScope.Root,
        default="",
        help="Display name used in greetings",
    )

    TabMaxLength = IntRangeOptionSpec(
        name="tab_max_length",
        scope=OptionScope.Root,
        default=20,
        help="Maximum characters for tab names",
        min=10,
        max=50,
    )

    CommitSelectable = ChoicesOptionSpec(
        name="commit_selectable",
        scope=OptionScope.Session,
        default="learn_only",
        help="Which messages can be selected in commit mode",
        choices=["learn_only", "all_agent", "all"],
    )

    ToolUseVisibility = ChoicesOptionSpec(
        name="tool_use_visibility",
        scope=OptionScope.Session,
        default="debug",
        help="Minimum visibility level for tool calls to appear in the UI",
        choices=["debug", "default", "essential_only"],
    )

    class Agent(OptionNamespace):
        name = "agent"

        Provider = ChoicesOptionSpec(
            name="provider",
            scope=OptionScope.Session,
            default="anthropic",
            help="LLM provider",
            choices=["anthropic", "openai"],
        )

        Model = ConditionalChoicesOptionSpec(
            name="model",
            scope=OptionScope.Session,
            help="LLM model for the agent",
            condition=Provider,
            choices={
                "anthropic": [
                    "claude-opus-4-7",
                    "claude-opus-4-6",
                    "claude-sonnet-4-6",
                    "claude-haiku-4-5",
                ],
                "openai": [
                    "gpt-5.2",
                    "gpt-5-mini",
                    "gpt-5-nano",
                ],
            },
            defaults={
                "anthropic": "claude-opus-4-7",
                "openai": "gpt-5-mini",
            },
        )

        Temperature = FloatRangeOptionSpec(
            name="temperature",
            scope=OptionScope.Session,
            default=0.3,
            help="Sampling temperature for LLM responses",
            min=0.0,
            max=1.0,
            step=0.1,
        )

        AnswerVerbosity = ChoicesOptionSpec(
            name="answer_verbosity",
            scope=OptionScope.Session,
            default="auto",
            help="Controls response length and detail level",
            choices=["terse", "standard", "verbose", "auto"],
        )

        PlanningVerbosity = ChoicesOptionSpec(
            name="planning_verbosity",
            scope=OptionScope.Session,
            default="low",
            help="Controls how much the agent narrates its tool-call plans",
            choices=["low", "medium", "high"],
        )

        ParallelToolCalling = ToggleOptionSpec(
            name="parallel_tool_calling",
            scope=OptionScope.Session,
            default="enabled",
            help=(
                "Allow the LLM to issue multiple tool calls per response. "
                "All tools are designed to run concurrently, so this rarely needs to be disabled."
            ),
        )

        class Anthropic(OptionNamespace):
            name = "anthropic"
            description = "Only used when agent.provider is anthropic."

            PromptCache = ToggleOptionSpec(
                name="prompt_cache",
                scope=OptionScope.Session,
                default="enabled",
                help="Whether to include Anthropic cache-control breakpoints in messages.",
            )
            PromptCacheTTL = ChoicesOptionSpec(
                name="prompt_cache_ttl",
                scope=OptionScope.Session,
                default="5m",
                help="TTL for Anthropic prompt cache (if enabled)",
                choices=["5m", "1h"],
            )

            WebTools = ToggleOptionSpec(
                name="web_tools",
                scope=OptionScope.Session,
                default="disabled",
                help="Enable Anthropic server-side web_search and web_fetch tools",
            )

    class Subagents(OptionNamespace):
        name = "subagents"

        class Commit(OptionNamespace):
            name = "commit"
            description = "Options related to the construction, management, and routing of the commit subagent."

            Enabled = ToggleOptionSpec(
                name="enabled",
                scope=OptionScope.Session,
                default="enabled",
                help=(
                    "Whether the commit subagent is available for delegation, otherwise the "
                    "root conversation agent handles drafting commit proposals, explicitly."
                ),
            )

            RoutingCriterion = ChoicesOptionSpec(
                name="routing_criterion",
                scope=OptionScope.Session,
                default="tokens",
                help="Criterion used to decide when to delegate to the commit subagent",
                choices=["tokens", "messages"],
            )

            RoutingThreshold = ConditionalIntRangeOptionSpec(
                name="routing_threshold",
                scope=OptionScope.Session,
                help="Threshold above which the commit subagent is used",
                condition=RoutingCriterion,
                ranges={
                    "tokens": (0, 10000),
                    "messages": (0, 20),
                },
                defaults={
                    "tokens": 2000,
                    "messages": 10,
                },
            )

    # ---- Instance ----

    def __init__(self, scope: OptionScope, parent: Options | None = None) -> None:
        super().__init__()
        self._scope = scope
        self._parent = parent
        self._logger = get_logger("tui.options")
        self._children: list[Options] = []
        self._values: dict[str, Any] = {}

        self.make_callback_groups({
            self.Callbacks.OnChanged: (OptionScope, OptionSpec, object, object),
            self.Callbacks.OnBatchUpdated: (dict, dict),
        })

        # Link into the parent/child hierarchy. ``_children`` is also the strong reference that
        # keeps the parent's weakly-held forwarding subscription (below) alive for our lifetime.
        if parent is not None:
            parent._children.append(self)
            parent.subscribe(parent.Callbacks.OnChanged, self._forward_changed)

        # At the root scope, all options are initialized to their defaults.
        # At child scopes, values are inherited from the parent unless explicitly overridden.
        if scope == OptionScope.Root:
            for s in self.spec():
                self._values[s.resolved_name] = s.default

        # When a condition option changes, reset its conditional dependents. One bound-method
        # subscriber covers every dependent (it survives weak-ref dispatch because ``self`` does).
        self.subscribe(self.Callbacks.OnChanged, self._cascade_conditionals)

        # Relay each change into its per-spec group, powering ``subscribe_on_changed``.
        self.subscribe(self.Callbacks.OnChanged, self._fanout_to_spec_groups)

    # -- Read --

    @property
    def scope(self) -> OptionScope:
        """The minimum scope at which this instance accepts mutations."""
        return self._scope

    def get(self, spec: OptionSpec) -> Any:
        """Resolve a value: local override → parent chain → default."""
        if spec.resolved_name in self._values:
            return self._values[spec.resolved_name]
        if self._parent is not None:
            return self._parent.get(spec)
        return spec.default

    def inject(self, fn: Callable) -> Callable:
        """Curry ``fn``'s option-annotated parameters against this instance, returning a callable over the
        remaining parameters. ``Annotated[T, spec]`` binds the current value (a snapshot); ``Annotated[
        OptionRef[T], spec]`` binds a live ``OptionRef``.

        Lenient like ``ServiceAccessor.inject``: a parameter that is not option-annotated is left open for
        the caller, which is what lets the two compose (services in one pass, options in the other). See
        the module-level injection section and the ``option_bindings`` reader.
        """
        bound: dict[str, Any] = {}
        for name, param in inspect.signature(fn).parameters.items():
            if name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            binding = _option_binding(param.annotation)
            if binding is not None:
                spec, live = binding
                bound[name] = OptionRef(self, spec) if live else self.get(spec)
        return functools.partial(fn, **bound)

    # -- Write --

    def set(self, spec: OptionSpec, value: Any, *, flush: bool = True) -> None:
        """Validate and set *value*, emitting ``OnChanged`` if it actually moved."""
        if spec.scope < self._scope:
            raise ValueError(
                f"Cannot set {spec.resolved_name} at {self._scope.name} scope "
                f"(minimum scope: {spec.scope.name})"
            )
        old = self.get(spec)
        if isinstance(spec, ConditionalChoicesOptionSpec):
            condition_value = self.get(spec.condition)
            value = spec.validate(value, condition_value=condition_value)
        else:
            value = spec.validate(value)
        self._values[spec.resolved_name] = value
        if old != value:
            self._logger.info("Option %s changed: %r → %r", spec.resolved_name, old, value)
            self.emit(self.Callbacks.OnChanged, self._scope, spec, old, value)
        if flush:
            self.flush()

    def reset(self, spec: OptionSpec, *, flush: bool = True) -> None:
        """Remove a local override (session) or reset to default (root)."""
        if self._scope == OptionScope.Root:
            self.set(spec, spec.default, flush=flush)
        else:
            old = self.get(spec)
            self._values.pop(spec.resolved_name, None)
            new = self.get(spec)
            if old != new:
                self.emit(self.Callbacks.OnChanged, self._scope, spec, old, new)
            if flush:
                self.flush()

    def _forward_changed(self, scope: OptionScope, spec: OptionSpec, old: Any, new: Any) -> None:
        """Re-emit a parent-scope change into this scope so our observers and cascades see it.

        Skipped when we locally override ``spec`` — a child override takes precedence over the
        parent value, so an inherited change to it is intentionally invisible here. ``scope`` is
        forwarded unchanged: it names where the change originated, not where it's being relayed.
        """
        if spec.resolved_name in self._values:
            return
        self.emit(self.Callbacks.OnChanged, scope, spec, old, new)

    def _cascade_conditionals(self, scope: OptionScope, spec: OptionSpec, old: Any, new: Any) -> None:
        """When a condition option changes, reset its conditional dependents to the branch default.

        Runs on every ``OnChanged`` (the cost is a spec scan) and fires per scope: because parent
        changes are forwarded into child scopes, each inheriting scope resets its own dependents.
        """
        for dep in self.spec():
            if isinstance(dep, ConditionalChoicesOptionSpec) and dep.condition is spec:
                if self.get(dep) not in dep.choices_for(new):
                    self.set(dep, dep.defaults[new], flush=True)
            elif isinstance(dep, ConditionalIntRangeOptionSpec) and dep.condition is spec:
                # Always reset to the branch default. Unlike discrete choices where values from one
                # branch are typically invalid in another, integer ranges often overlap (e.g. 10 is
                # valid in both 0-20 and 0-10000) — so an out-of-range check isn't sufficient.
                self.set(dep, dep.defaults[new], flush=True)

    # -- Transactional staging --

    def clone(self) -> Options:
        """Return a detached snapshot at the same scope, seeded with current resolved values.

        The returned instance has no parent link and no external subscribers — only the
        conditional auto-cascade subscriptions ``__init__`` wires internally. Editor surfaces
        use this as a scratch space: stage edits via ``clone.set(...)`` (which fires the
        clone's own internal cascades but no one else's), then commit in one shot via
        ``target.merge_from(clone)``.

        Every spec is materialized into ``_values`` so the clone is fully self-contained
        regardless of source scope. A side effect at session scope is that the clone no
        longer presents the parent-inheritance distinction (every spec reads as a local
        override) — fine for transactional staging, where the only consumer is the editor.
        """
        snap = type(self)(self._scope)
        for spec in self.spec():
            snap._values[spec.resolved_name] = self.get(spec)
        return snap

    def merge_from(self, other: Options) -> dict[str, tuple[Any, Any]]:
        """Pull ``other``'s values into self via ``self.set()`` for every spec where they differ.

        Conditions land before their dependents (see ``_ordered_for_merge``) so the dependent's
        scope-aware validation sees the new branch when it runs. A single ``flush()`` plus
        ``post_update()`` runs at the end, only if anything actually changed — callers don't
        pay the per-set JSONC flush cost and ``OnBatchUpdated`` subscribers see one coalesced
        tick. Returns ``{resolved_name: (old, new)}`` for what actually moved, with ``old``
        snapshotted pre-merge so cascade-driven transitions are reported against the user's
        starting state rather than against an intermediate one.
        """
        # Snapshot pre-merge state. Without this, a spec whose value flipped via a cascade
        # triggered by an earlier set() in this same merge would report its ``old`` as the
        # intermediate (post-cascade) value, which is misleading. The full snapshot also serves
        # as the ``OnBatchUpdated`` ``prev`` payload.
        initial = self._snapshot()

        for spec in self._ordered_for_merge():
            # Defensive: skip specs ``other`` carries that self can't actually set (e.g. a Root
            # spec being merged into a Session instance). In practice ``other`` is always a
            # clone of self, so this is dead code — but the public method signature doesn't
            # enforce that.
            if spec.scope < self._scope:
                continue
            new = other.get(spec)
            if self.get(spec) == new:
                continue
            self.set(spec, new, flush=False)

        changes: dict[str, tuple[Any, Any]] = {}
        for spec in self.spec():
            if spec.scope < self._scope:
                continue
            old = initial[spec.resolved_name]
            new = self.get(spec)
            if old != new:
                changes[spec.resolved_name] = (old, new)

        if changes:
            self.flush()
            self.post_update(prev=initial)

        return changes

    def _ordered_for_merge(self) -> list[OptionSpec]:
        """Topo order: condition specs before their dependents.

        Without this, a merge that flips both ``provider`` and ``model`` simultaneously could
        process ``model`` first against the OLD provider — and reject a value valid only in
        the new branch.
        """
        cond_of: dict[OptionSpec, OptionSpec] = {
            s: s.condition
            for s in self.spec()
            if isinstance(s, (ConditionalChoicesOptionSpec, ConditionalIntRangeOptionSpec))
        }
        seen: set[OptionSpec] = set()
        out: list[OptionSpec] = []

        def visit(spec: OptionSpec) -> None:
            if spec in seen:
                return
            seen.add(spec)
            if spec in cond_of:
                visit(cond_of[spec])
            out.append(spec)

        for spec in self.spec():
            visit(spec)
        return out

    # -- Events / batch updates --

    def subscribe_on_changed(self, spec: OptionSpec, handler: Callable[[Any, Any], None]) -> None:
        """Watch a single spec: ``handler(prev, new)`` fires only for *its* ``OnChanged``.

        A convenience over subscribing to ``OnChanged`` and filtering on ``key`` yourself. Each
        spec gets its own ``CallbackGroup``; an internal relay forwards every change into it, so
        inherited (parent-scope) changes reach the handler too. The handler is held by weak
        reference exactly like any ``CallbackHost`` subscriber — pass a bound method of a long-lived
        object, not a throwaway closure, or it will be collected immediately. (Need the originating
        ``scope``? Subscribe to ``Callbacks.OnChanged`` directly for the full ``(scope, key, prev,
        new)`` payload.)
        """
        if spec not in self._callbacks:
            self.make_callback_group(spec)
        self.subscribe(spec, handler)

    def unsubscribe_on_changed(self, spec: OptionSpec, handler: Callable[[Any, Any], None]) -> None:
        """Remove a ``subscribe_on_changed`` handler. No-op if the spec was never subscribed."""
        if spec in self._callbacks:
            self.unsubscribe(spec, handler)

    def _fanout_to_spec_groups(self, scope: OptionScope, spec: OptionSpec, prev: Any, new: Any) -> None:
        """Relay an ``OnChanged`` into the spec's own group, if any ``subscribe_on_changed`` exists."""
        group = self._callbacks.get(spec)
        if group is not None:
            self.emit(group, prev, new)

    def _snapshot(self) -> dict[str, Any]:
        """Effective values for every spec, keyed by resolved name — the ``OnBatchUpdated`` payload."""
        return {s.resolved_name: self.get(s) for s in self.spec()}

    def post_update(self, prev: dict[str, Any] | None = None) -> None:
        """Emit ``OnBatchUpdated`` with before/after snapshots, then recurse to children.

        ``prev`` is the pre-batch snapshot (``merge_from`` passes its own); when omitted, callers
        signalling a single settled change get ``prev == new`` (current state, no diff). Each child
        re-emits with *its own* effective snapshot so child-scope observers see correct values.
        """
        new = self._snapshot()
        self.emit(self.Callbacks.OnBatchUpdated, prev if prev is not None else new, new)
        for child in self._children:
            child.post_update()

    def detach(self) -> None:
        """Unsubscribe from the parent's events and drop the parent/child link."""
        if self._parent is not None:
            self._parent.unsubscribe(self._parent.Callbacks.OnChanged, self._forward_changed)
            try:
                self._parent._children.remove(self)
            except ValueError:
                pass
            self._parent = None

    # -- Spec registry --

    @classmethod
    def spec(cls) -> list[OptionSpec]:
        """Flat list of all ``OptionSpec`` instances defined on the class."""
        return list(cls._all_specs)  # type: ignore[attr-defined]

    @classmethod
    def spec_tree(cls) -> tuple[list[OptionSpec], list[OptionNamespaceNode]]:
        """Hierarchical spec tree: ``(top_level_options, namespace_nodes)``."""
        return cls._spec_tree  # type: ignore[attr-defined]

    # -- JSONC persistence --

    def flush(self) -> None:
        """Write values to the JSONC config file (root scope only)."""
        if self._scope != OptionScope.Root:
            return
        path = get_options_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        all_specs = self.spec()
        last_resolved = all_specs[-1].resolved_name if all_specs else ""
        top_level, nodes = self.spec_tree()

        lines = ["{"]
        self._flush_specs(lines, top_level, "    ", last_resolved)
        for node in nodes:
            self._flush_node(lines, node, "    ", last_resolved)
        lines.append("}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._logger.debug("Options saved to %s", path)

    def _flush_specs(
        self,
        lines: list[str],
        specs: list[OptionSpec],
        indent: str,
        last_resolved: str,
    ) -> None:
        for s in specs:
            for comment_line in s.jsonc_comment().splitlines():
                if comment_line.startswith("//"):
                    lines.append(f"{indent}{comment_line}")
                else:
                    lines.append(f"{indent}// {comment_line}")
            value = self._values.get(s.resolved_name, s.default)
            json_val = json.dumps(value)
            comma = "," if s.resolved_name != last_resolved else ""
            lines.append(f"{indent}{json.dumps(s.resolved_name)}: {json_val}{comma}")
            if s.resolved_name != last_resolved:
                lines.append("")

    def _flush_node(
        self,
        lines: list[str],
        node: OptionNamespaceNode,
        indent: str,
        last_resolved: str,
    ) -> None:
        ns = node.namespace
        if ns.description:
            lines.append(f"{indent}// {ns.description}")
        self._flush_specs(lines, node.options, indent, last_resolved)
        for child in node.children:
            self._flush_node(lines, child, indent, last_resolved)

    @classmethod
    def load(cls) -> Options:
        """Load from the JSONC config file, returning a Root-scope instance."""
        instance = cls(OptionScope.Root)
        path = get_options_path()
        if not path.exists():
            instance._logger.info("No options file found, using defaults")
            return instance
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(_strip_comments(raw))
        except (json.JSONDecodeError, OSError):
            return instance

        spec_map = {s.resolved_name: s for s in cls.spec()}
        for key, val in data.items():
            s = spec_map.get(key)
            if s is None:
                continue
            try:
                instance._values[s.resolved_name] = s.validate(val)
            except (ValueError, TypeError):
                pass  # keep default
        instance._logger.info("Options loaded from %s", path)
        return instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """Remove ``//`` comment lines from JSONC text."""
    return "\n".join(
        line for line in text.splitlines()
        if not re.match(r"^\s*//", line)
    )


def build_jsonc_snapshot(target: Options) -> str:
    """Build a JSONC string from the spec tree for external editor use."""
    all_specs = [s for s in Options.spec() if s.scope >= target._scope]
    last_resolved = all_specs[-1].resolved_name if all_specs else ""
    top_level, nodes = Options.spec_tree()

    lines = ["{"]

    def _emit_specs(specs: list, indent: str = "    ") -> None:
        for s in [s for s in specs if s.scope >= target._scope]:
            for comment_line in s.jsonc_comment().splitlines():
                if comment_line.startswith("//"):
                    lines.append(f"{indent}{comment_line}")
                else:
                    lines.append(f"{indent}// {comment_line}")
            value = target.get(s)
            json_val = json.dumps(value)
            comma = "," if s.resolved_name != last_resolved else ""
            lines.append(f"{indent}{json.dumps(s.resolved_name)}: {json_val}{comma}")
            if s.resolved_name != last_resolved:
                lines.append("")

    def _emit_node(node: OptionNamespaceNode, indent: str = "    ") -> None:
        ns = node.namespace
        if ns.description:
            lines.append(f"{indent}// {ns.description}")
        _emit_specs(node.options, indent)
        for child in node.children:
            _emit_node(child, indent)

    _emit_specs(top_level)
    for node in nodes:
        _emit_node(node)

    lines.append("}")
    return "\n".join(lines) + "\n"


def parse_jsonc(text: str) -> dict[str, Any]:
    """Parse a JSONC string, validating values against the spec registry."""
    data = json.loads(_strip_comments(text))
    spec_map = {s.resolved_name: s for s in Options.spec()}
    result: dict[str, Any] = {}
    for key, val in data.items():
        s = spec_map.get(key)
        if s is not None:
            result[key] = s.validate(val)
    return result
