"""Callback groups and emitters: the subscription primitive underneath the app's event channels.

``CallbackHost`` is a base class for any object that owns named callback groups — view-models, but
also plain model objects (graphs, stores) whose consumers want push notification without polling.
The machinery is deliberately small:

- ``CallbackGroup`` — a named group of weakly-held subscribers, owned by exactly one host.
- ``CallbackHost.emit/subscribe/unsubscribe`` — immediate fan-out by group or by registered key.
- ``CallbackHost.emit_once`` — a context manager yielding an ``Emitter`` that coalesces repeat emits
  of the same group within a synchronous block, firing each group at most once on exit.

Subscribers are held by weak reference: a garbage-collected subscriber is pruned on the next
dispatch, so a forgotten unsubscribe never leaks the subscriber. The flip side: the callable must be
weak-referenceable and something other than the subscription must keep it alive. Bound methods of
long-lived objects (the common case) satisfy both; a bare lambda passed to ``subscribe`` dies
immediately unless stored elsewhere first.

Dispatch isolates exceptions — a raising subscriber is logged and skipped, the rest of the group
still fires.
"""

from __future__ import annotations

import logging
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Hashable, ParamSpec, overload


logger = logging.getLogger(__name__)


P = ParamSpec("P")


def _make_weak_ref(cb: Callable[..., Any]) -> Callable[[], Callable[..., Any] | None]:
    """Wrap ``cb`` as a callable that returns ``cb`` while it's alive, or ``None`` once GC'd.

    Bound methods need ``weakref.WeakMethod`` — a plain ``weakref.ref`` on ``obj.method`` would hold
    the temporary bound-method object, which dies immediately after the subscribe call returns.

    Falls back to a strong reference for callables that can't be weak-referenced (e.g. some
    ``functools.partial`` instances, builtins). Subscribers using those must unsubscribe explicitly.
    """
    if hasattr(cb, "__self__") and hasattr(cb, "__func__"):
        return weakref.WeakMethod(cb)
    try:
        return weakref.ref(cb)
    except TypeError:
        return lambda: cb


@dataclass(frozen=True)
class CallbackGroup(Generic[P]):
    """A named group of callbacks owned by a single ``CallbackHost`` instance.

    ``key`` identifies the group within its owning host (typically a value from that host's
    ``Callbacks`` namespace). ``owner_id`` is ``id(host)`` of the owning host — used by ``Emitter``
    to enforce single-host batching. ``_refs`` is the list of weak references to subscribed
    callbacks; dead entries are pruned lazily during dispatch.

    ``P`` parameterizes the callback signature. Use ``CallbackGroup[[]]`` for nullary groups,
    ``CallbackGroup[[Card]]`` for a single ``Card`` payload, ``CallbackGroup[[int, str]]`` for two
    positional args, and so on. The type flows through ``emit`` (``*args: P.args, **kwargs:
    P.kwargs``) and ``subscribe`` (``Callable[P, None]``) so the type checker rejects mismatched
    payloads at both ends.

    Subscribers are held by weak reference — see the module docstring for the lifetime rules.
    Explicit ``unsubscribe`` remains useful for eager teardown: preventing a not-yet-collected
    subscriber from receiving one last callback.

    Construct via ``CallbackHost.make_callback_group(key)`` rather than directly — the helper sets
    ``owner_id`` correctly.
    """
    key: Hashable
    owner_id: int
    _refs: list[Callable[[], Callable[P, None] | None]] = field(
        default_factory=list, compare=False, hash=False
    )


def _dispatch(group: CallbackGroup[P], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Fire all live callbacks on ``group``, isolate exceptions, and prune dead entries.

    Dereferences each stored weakref; ``None`` results (GC'd subscribers) are queued for removal at
    the end. Surviving callbacks run inside a try/except so one bad subscriber can't break the
    fan-out for the rest — exceptions are logged with full traceback via ``logger.exception``.

    Called by ``CallbackHost.emit`` (immediate fire), ``Emitter.emit`` (immediate fall-through for
    non-batched groups), and ``Emitter._flush`` (deferred fire at end of ``emit_once`` block).
    """
    dead: list[int] = []
    for i, ref in enumerate(group._refs):
        cb = ref()
        if cb is None:
            dead.append(i)
            continue
        try:
            cb(*args, **kwargs)
        except Exception:
            logger.exception(
                "callback for group %r raised; continuing with remaining subscribers", group.key
            )
    for i in reversed(dead):
        del group._refs[i]


def _resolve_group(
    host: CallbackHost, group_or_key: CallbackGroup[P] | Hashable
) -> CallbackGroup[P]:
    """Coerce a ``CallbackGroup`` or a ``Hashable`` key into the underlying ``CallbackGroup``.

    Passing a ``CallbackGroup`` returns it unchanged (the caller already has the object). Passing a
    bare key looks it up in ``host._callbacks`` — the registry populated by
    ``CallbackHost.make_callback_group``. Missing keys raise ``KeyError`` with the available keys
    listed for easier debugging.
    """
    if isinstance(group_or_key, CallbackGroup):
        return group_or_key
    try:
        return host._callbacks[group_or_key]
    except KeyError:
        raise KeyError(
            f"No CallbackGroup registered under key {group_or_key!r}. "
            f"Available keys: {list(host._callbacks.keys())}"
        ) from None


class Emitter:
    """Single-host emitter handed out by ``CallbackHost.emit_once``. Captures emits for matching
    groups within its scope and replays them once on exit. Emits for non-matching groups fall
    through immediately. Inert after exit.

    Emitters are tied to the host that created them. Passing a CallbackGroup owned by a different
    host to ``emitter.emit(...)`` raises ``ValueError`` — by design: each host owns its own events
    and emits them through its own methods, so cross-host coordination happens via direct method
    calls (each callee emitting *its own* groups), never via shared emitters. We fail loudly on the
    attempt rather than silently coalescing distinct hosts' groups under the same key. (For the
    view-model expression of this rule, see ``ViewModelBase``.)

    Both ``emit`` and the batched-groups list accept either a ``CallbackGroup`` directly or a bare
    ``Hashable`` key. Keys are resolved against the owning host's ``_callbacks`` registry — handy
    for plumbing through ``Callbacks`` namespace values without first reaching for the attribute.
    """

    class MergeStrategy(Enum):
        STRICT = "strict"  # raise on conflicting args for the same group
        LAST = "last"      # last emit wins
        FIRST = "first"    # first emit wins

    def __init__(
        self,
        host: CallbackHost,
        groups: tuple[CallbackGroup[Any] | Hashable, ...] | None = None,
        merge_strategy: MergeStrategy = MergeStrategy.STRICT,
    ) -> None:
        self._host = host
        self._owner_id = id(host)
        # None means "batch every group". Otherwise, restrict batching to these keys. A
        # ``CallbackGroup`` contributes its ``.key``; a bare ``Hashable`` is used as-is.
        self._batched_keys: frozenset[Hashable] | None = (
            None
            if groups is None
            else frozenset(g.key if isinstance(g, CallbackGroup) else g for g in groups)
        )
        self._merge_strategy = merge_strategy
        # Keyed by CallbackGroup.key. Stores (group, args, kwargs) of the captured call; behavior on
        # repeat depends on merge_strategy.
        self._pending: dict[
            Hashable, tuple[CallbackGroup[Any], tuple[Any, ...], dict[str, Any]]
        ] = {}
        self._closed = False

    def _is_batched(self, group: CallbackGroup[Any]) -> bool:
        return self._batched_keys is None or group.key in self._batched_keys

    @overload
    def emit(self, group: CallbackGroup[P], *args: P.args, **kwargs: P.kwargs) -> None: ...
    @overload
    def emit(self, key: Hashable, *args: Any, **kwargs: Any) -> None: ...
    def emit(self, group_or_key, *args, **kwargs) -> None:
        if self._closed:
            raise RuntimeError(
                "Cannot emit through an Emitter after its emit_once block has exited."
            )

        group = _resolve_group(self._host, group_or_key)

        if group.owner_id != self._owner_id:
            raise ValueError(
                f"Emitter is owned by host id={self._owner_id} but received a CallbackGroup owned "
                f"by host id={group.owner_id}. Emitters are single-host scoped: cross-host "
                f"coordination happens through direct method calls, not shared emitters."
            )

        # Non-batched group: fall through immediately.
        if not self._is_batched(group):
            _dispatch(group, args, kwargs)
            return

        existing = self._pending.get(group.key)
        if existing is None:
            self._pending[group.key] = (group, args, kwargs)
            return

        _, prev_args, prev_kwargs = existing
        same_args = (prev_args, prev_kwargs) == (args, kwargs)

        if same_args:
            # Identical repeat — coalesce silently regardless of strategy.
            return

        if self._merge_strategy is Emitter.MergeStrategy.STRICT:
            raise ValueError(
                f"emit_once block received conflicting args for CallbackGroup(key={group.key!r}): "
                f"{(prev_args, prev_kwargs)!r} vs {(args, kwargs)!r}"
            )
        elif self._merge_strategy is Emitter.MergeStrategy.LAST:
            self._pending[group.key] = (group, args, kwargs)
        elif self._merge_strategy is Emitter.MergeStrategy.FIRST:
            pass  # keep existing
        else:
            raise AssertionError(f"Unknown merge strategy: {self._merge_strategy}")

    def _flush(self) -> None:
        """Fire one emit per captured group, then mark closed."""
        try:
            for group, args, kwargs in self._pending.values():
                _dispatch(group, args, kwargs)
        finally:
            self._closed = True
            self._pending.clear()


class CallbackHost:
    """Base class for objects that own callback groups and emit through them.

    Subclasses declare their keys (conventionally a nested plain ``Callbacks`` class of string
    constants), register groups in ``__init__`` via ``make_callback_groups``, and fire them with
    ``emit``. Consumers attach with ``subscribe`` — by group object or by key:

        class TopicStore(CallbackHost):
            class Callbacks:
                OnTopicAdded   = "OnTopicAdded"
                OnTopicDeleted = "OnTopicDeleted"

            def __init__(self):
                super().__init__()
                self.make_callback_groups({
                    self.Callbacks.OnTopicAdded:   int,   # payload-shape documentation only
                    self.Callbacks.OnTopicDeleted: int,
                })

    Groups are reached by key at emit / subscribe sites — no ``self._foo`` attribute storage
    required. Subclasses that want typed access can layer a ``@property`` over
    ``self._callbacks[key]`` to recover the ``CallbackGroup[P]`` annotation.
    """

    def __init__(self) -> None:
        # Registry of every group created via ``make_callback_group``, keyed by the registration key.
        self._callbacks: dict[Hashable, CallbackGroup[Any]] = {}

    def make_callback_group[**P](self, key: Hashable) -> CallbackGroup[P]:
        """Construct a CallbackGroup owned by this host and register it in ``self._callbacks`` under
        ``key``. Subclasses use this to build their own groups so that ``owner_id`` is set correctly,
        the single-host emitter check fires on cross-host misuse, and the group is reachable by key.

        Generic over the callback signature ``P``: annotate the attribute at the call site and the
        type checker narrows ``P`` from the assignment target — e.g.

            self._foo: CallbackGroup[[int, str]] = self.make_callback_group(Callbacks.FOO)

        gives ``self._foo`` the expected ``CallbackGroup[[int, str]]`` type. Omitting the annotation
        leaves ``P`` unsolved; for nullary groups this is harmless, for groups with payloads it
        loses the end-to-end signature check that the rest of the machinery is designed to give you.

        Raises ``ValueError`` on duplicate registration — every ``CallbackGroup`` owned by a host
        must have a unique key.
        """
        if key in self._callbacks:
            raise ValueError(
                f"CallbackGroup with key {key!r} is already registered on this host. Each key may "
                "be used at most once per host instance."
            )
        group: CallbackGroup[P] = CallbackGroup(key=key, owner_id=id(self))
        self._callbacks[key] = group
        return group

    def make_callback_groups(self, specs: dict[Hashable, Any]) -> None:
        """Bulk-register a set of CallbackGroups by key. The values in ``specs`` are payload-shape
        documentation only — Python's type system can't propagate a dict literal's values into
        per-key ``ParamSpec``s, so the type checker won't enforce them. Convention:

        - ``None`` — nullary group (no emit args)
        - a type ``T`` — single-arg group; ``emit(key, t)``
        - a tuple ``(T1, T2, ...)`` — multi-arg group

        Returns nothing — groups land in ``self._callbacks`` via ``make_callback_group``; duplicate
        keys raise ``ValueError`` per that helper.
        """
        for key in specs:
            self.make_callback_group(key)

    @overload
    def emit(self, group: CallbackGroup[P], *args: P.args, **kwargs: P.kwargs) -> None: ...
    @overload
    def emit(self, key: Hashable, *args: Any, **kwargs: Any) -> None: ...
    def emit(self, group_or_key, *args, **kwargs) -> None:
        """Fire an emit immediately. Bypasses any active ``emit_once`` blocks — those only capture
        emits routed through their emitter object.

        Accepts either a ``CallbackGroup`` or a bare ``Hashable`` key (looked up in
        ``self._callbacks``). The key form is the same one passed to ``make_callback_group``, so a
        ``Callbacks`` namespace value works directly without reaching for the attribute.

        No owner_id check is performed here: ``self.emit`` is just an immediate fan-out to
        subscribers, with no key-coalescing semantics that cross-host groups could corrupt. The
        single-host enforcement applies to emitters yielded by ``emit_once`` only.
        """
        group = _resolve_group(self, group_or_key)
        _dispatch(group, args, kwargs)

    @overload
    def subscribe(self, group: CallbackGroup[P], callback: Callable[P, None]) -> None: ...
    @overload
    def subscribe(self, key: Hashable, callback: Callable[..., None]) -> None: ...
    def subscribe(self, group_or_key, callback) -> None:
        """Subscribe ``callback`` to a group (or to the group identified by ``key``). The callback is
        held by weak reference; the caller must keep its own strong reference (typically by passing a
        bound method of a long-lived object). A bare lambda will die immediately.
        """
        group = _resolve_group(self, group_or_key)
        group._refs.append(_make_weak_ref(callback))

    @overload
    def unsubscribe(self, group: CallbackGroup[P], callback: Callable[P, None]) -> None: ...
    @overload
    def unsubscribe(self, key: Hashable, callback: Callable[..., None]) -> None: ...
    def unsubscribe(self, group_or_key, callback) -> None:
        """Remove ``callback`` from a group (or from the group identified by ``key``). No-op if not
        subscribed. Bound-method equality is structural (same ``__self__`` and ``__func__``), so
        passing the same bound method again unsubscribes the original subscription.
        """
        group = _resolve_group(self, group_or_key)
        for i, ref in enumerate(group._refs):
            if ref() == callback:
                del group._refs[i]
                return

    @contextmanager
    def emit_once(
        self,
        *groups_or_keys: CallbackGroup[Any] | Hashable,
        merge_strategy: Emitter.MergeStrategy = Emitter.MergeStrategy.STRICT,
    ):
        """Yield an emitter that batches ``emitter.emit(group, ...)`` calls for the given groups,
        firing each at most once on exit.

        Each batched-group argument may be a ``CallbackGroup`` or a bare ``Hashable`` key. If no
        groups are passed, every emit through the emitter is batched. Emits through the emitter for
        groups not in the list fall through immediately. Emits made via ``self.emit(...)`` directly,
        or through a different emitter, are unaffected.

        The yielded emitter is tied to *this* host only — passing a CallbackGroup owned by another
        host raises ``ValueError``. See the ``Emitter`` docstring for the rationale.

        merge_strategy controls behavior when the same group is emitted multiple times with differing
        args within the block:
          - STRICT (default): raise ValueError on conflict
          - LAST: last emit's args win
          - FIRST: first emit's args win
        Identical repeats coalesce silently under all strategies.
        """
        emitter = Emitter(
            host=self,
            groups=groups_or_keys if groups_or_keys else None,
            merge_strategy=merge_strategy,
        )
        try:
            yield emitter
        finally:
            emitter._flush()
