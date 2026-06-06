"""Option definitions, persistence, validation, and pub/sub for the TUI.

Provides a hierarchical options system with scoped inheritance (Root → Session),
validation, JSONC persistence, and async subscriber notifications.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, overload

from rhizome.config import get_options_path
from rhizome.logs import get_logger


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

EventHandler = Callable[[Any, Any], Awaitable[None]]


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
# Options
# ---------------------------------------------------------------------------


class Options(metaclass=OptionsMeta):
    """Hierarchical, scoped option store with pub/sub and JSONC persistence.

    **Class-level**: ``OptionSpec`` and ``OptionNamespace`` members define the
    schema (wired by ``OptionsMeta``).

    **Instance-level**: holds ``_values``, manages subscriptions and
    parent/child links.
    """

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
        self._scope = scope
        self._parent = parent
        self._logger = get_logger("tui.options")
        self._children: list[Options] = []
        self._values: dict[str, Any] = {}
        self._subscribers: dict[OptionSpec, list[EventHandler]] = {}
        self._post_update_subscribers: list[Callable[[Options], Awaitable[None]]] = []

        # Link into parent/child hierarchy
        if parent is not None:
            parent._children.append(self)

        # At the root scope, all options are initialized to their defaults.
        # At child scopes, values are inherited from the parent unless explicitly overridden.
        if scope == OptionScope.Root:
            for s in self.spec():
                self._values[s.resolved_name] = s.default

        # Auto-subscribe: when a condition option changes, reset dependents
        for s in self.spec():
            if isinstance(s, ConditionalChoicesOptionSpec):

                async def _on_condition_changed(
                    old: Any, new: Any, dep: ConditionalChoicesOptionSpec = s
                ) -> None:
                    current = self.get(dep)
                    valid = dep.choices_for(new)
                    if current not in valid:
                        await self.set(dep, dep.defaults[new], flush=True)

                self.subscribe(s.condition, _on_condition_changed)

            elif isinstance(s, ConditionalIntRangeOptionSpec):

                async def _on_range_condition_changed(
                    old: Any, new: Any, dep: ConditionalIntRangeOptionSpec = s
                ) -> None:
                    # Always reset to the branch default. Unlike discrete choices
                    # where values from one branch are typically invalid in another,
                    # integer ranges often overlap (e.g. 10 is valid in both 0-20
                    # and 0-10000) — so checking out-of-range isn't sufficient.
                    await self.set(dep, dep.defaults[new], flush=True)

                self.subscribe(s.condition, _on_range_condition_changed)

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

    # -- Write --

    async def set(self, spec: OptionSpec, value: Any, *, flush: bool = True) -> None:
        """Validate and set *value*, notifying subscribers."""
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
            for listener in self._subscribers.get(spec, []):
                await listener(old, value)
            await self._propagate_to_children(spec, old, value)
        if flush:
            self.flush()

    async def reset(self, spec: OptionSpec, *, flush: bool = True) -> None:
        """Remove a local override (session) or reset to default (root)."""
        if self._scope == OptionScope.Root:
            await self.set(spec, spec.default, flush=flush)
        else:
            old = self.get(spec)
            self._values.pop(spec.resolved_name, None)
            new = self.get(spec)
            if old != new:
                for listener in self._subscribers.get(spec, []):
                    await listener(old, new)
                await self._propagate_to_children(spec, old, new)
            if flush:
                self.flush()

    async def _propagate_to_children(
        self, spec: OptionSpec, old: Any, new: Any
    ) -> None:
        for child in self._children:
            # Only propagate if the child doesn't have a local override. Child options take
            # precedence over parent values, so if the child has an override we assume it's intentional and
            # don't propagate changes from the parent.
            if spec.resolved_name not in child._values:
                for listener in child._subscribers.get(spec, []):
                    await listener(old, new)
                await child._propagate_to_children(spec, old, new)

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

    async def merge_from(self, other: Options) -> dict[str, tuple[Any, Any]]:
        """Pull ``other``'s values into self via ``self.set()`` for every spec where they differ.

        Conditions land before their dependents (see ``_ordered_for_merge``) so the dependent's
        scope-aware validation sees the new branch when it runs. A single ``flush()`` plus
        ``post_update()`` runs at the end, only if anything actually changed — callers don't
        pay the per-set JSONC flush cost and downstream post-update subscribers see one
        coalesced tick. Returns ``{resolved_name: (old, new)}`` for what actually moved, with
        ``old`` snapshotted pre-merge so cascade-driven transitions are reported against the
        user's starting state rather than against an intermediate one.
        """
        # Snapshot pre-merge state. Without this, a spec whose value flipped via a cascade
        # triggered by an earlier set() in this same merge would report its ``old`` as the
        # intermediate (post-cascade) value, which is misleading.
        initial: dict[str, Any] = {
            spec.resolved_name: self.get(spec)
            for spec in self.spec()
            if spec.scope >= self._scope
        }

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
            await self.set(spec, new, flush=False)

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
            await self.post_update()

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

    # -- Subscriptions --

    @overload
    def subscribe(self, key: OptionSpec, listener: EventHandler) -> None: ...
    @overload
    def subscribe(self, key: OptionSpec, listener: list[EventHandler]) -> None: ...
    @overload
    def subscribe(self, key: dict[OptionSpec, EventHandler | list[EventHandler]]) -> None: ...  # type: ignore[override]

    def subscribe(self, key, listener=None):  # type: ignore[override]
        if isinstance(key, dict):
            for spec, handlers in key.items():
                if isinstance(handlers, list):
                    self._subscribers.setdefault(spec, []).extend(handlers)
                else:
                    self._subscribers.setdefault(spec, []).append(handlers)
        elif isinstance(listener, list):
            self._subscribers.setdefault(key, []).extend(listener)
        else:
            self._subscribers.setdefault(key, []).append(listener)

    def unsubscribe(self, spec: OptionSpec, listener: EventHandler) -> None:
        listeners = self._subscribers.get(spec, [])
        try:
            listeners.remove(listener)
        except ValueError:
            pass

    def subscribe_post_update(self, listener: Callable[[Options], Awaitable[None]]) -> None:
        """Register a callback invoked after a batch of option changes completes."""
        self._post_update_subscribers.append(listener)

    async def post_update(self) -> None:
        """Notify post-update subscribers, then propagate to children."""
        for listener in self._post_update_subscribers:
            await listener(self)
        for child in self._children:
            await child.post_update()

    def detach(self) -> None:
        """Remove this instance from parent's children list."""
        if self._parent is not None:
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
