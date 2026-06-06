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
    """A named group of callbacks owned by a single ``ViewModelBase`` instance.

    ``key`` identifies the group within its owning VM (typically a value from that VM's ``Callbacks``
    enum). ``owner_id`` is ``id(vm)`` of the VM that owns this group — used by ``Emitter`` to enforce
    single-VM batching. ``_refs`` is the list of weak references to subscribed callbacks; dead
    entries are pruned lazily during dispatch.

    ``P`` parameterizes the callback signature. Use ``CallbackGroup[[]]`` for nullary groups (the
    standard ``dirty`` / ``focus`` case), ``CallbackGroup[[Card]]`` for a single ``Card`` payload,
    ``CallbackGroup[[int, str]]`` for two positional args, and so on. The type flows through
    ``emit`` (``*args: P.args, **kwargs: P.kwargs``) and ``subscribe`` (``Callable[P, None]``) so
    the type checker rejects mismatched payloads at both ends.

    Subscribers are held by weak reference. Once a subscriber is garbage collected, its entry is
    pruned on the next dispatch. Explicit ``unsubscribe`` is still useful for eager teardown — so
    a not-yet-collected, unmounted view doesn't receive one last callback — but a forgotten
    unsubscribe no longer leaks the subscriber.

    Construct via ``ViewModelBase.make_callback_group(key)`` rather than directly — the helper sets
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

    Called by ``ViewModelBase.emit`` (immediate fire), ``Emitter.emit`` (immediate fall-through for
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
    model: ViewModelBase, group_or_key: CallbackGroup[P] | Hashable
) -> CallbackGroup[P]:
    """Coerce a ``CallbackGroup`` or a ``Hashable`` key into the underlying ``CallbackGroup``.

    Passing a ``CallbackGroup`` returns it unchanged (the caller already has the object). Passing a
    bare key looks it up in ``model._callbacks`` — the registry populated by
    ``ViewModelBase.make_callback_group``. Missing keys raise ``KeyError`` with the available keys
    listed for easier debugging.
    """
    if isinstance(group_or_key, CallbackGroup):
        return group_or_key
    try:
        return model._callbacks[group_or_key]
    except KeyError:
        raise KeyError(
            f"No CallbackGroup registered under key {group_or_key!r}. "
            f"Available keys: {list(model._callbacks.keys())}"
        ) from None


class Emitter:
    """Single-VM emitter handed out by ``ViewModelBase.emit_once``. Captures emits for matching groups
    within its scope and replays them once on exit. Emits for non-matching groups fall through
    immediately. Inert after exit.

    Emitters are tied to the VM that created them (held as ``self._model``, with ``self._owner_id``
    cached for the cross-VM check). Passing a CallbackGroup owned by a different VM to
    ``emitter.emit(...)`` raises ``ValueError`` — by design. Under the project's communication model
    (see ``ViewModelBase``):

    - Each VM owns its own events and emits them through its own methods.
    - Cross-VM coordination uses direct method calls, never shared emitters.
    - If a parent VM needs siblings to repaint, it calls public methods on them; each emits *its own*
      ``dirty`` independently.

    So an emitter never legitimately fires another VM's events — and we fail loudly if you try, rather
    than silently coalescing distinct VMs' groups under the same ``Callbacks`` enum value.

    Both ``emit`` and the batched-groups list accept either a ``CallbackGroup`` directly or a bare
    ``Hashable`` key. Keys are resolved against the owning VM's ``_callbacks`` registry — handy for
    plumbing through ``Callbacks`` enum values without first reaching for the attribute.
    """

    class MergeStrategy(Enum):
        STRICT = "strict"  # raise on conflicting args for the same group
        LAST = "last"      # last emit wins
        FIRST = "first"    # first emit wins

    def __init__(
        self,
        model: ViewModelBase,
        groups: tuple[CallbackGroup[Any] | Hashable, ...] | None = None,
        merge_strategy: MergeStrategy = MergeStrategy.STRICT,
    ) -> None:
        self._model = model
        self._owner_id = id(model)
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

        group = _resolve_group(self._model, group_or_key)

        if group.owner_id != self._owner_id:
            raise ValueError(
                f"Emitter is owned by VM id={self._owner_id} but received a CallbackGroup owned by "
                f"VM id={group.owner_id}. Emitters are single-VM scoped: cross-VM coordination happens "
                f"through direct method calls, not shared emitters. See ViewModelBase docstring."
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


class ViewModelBase:
    """Base class for MVVM view-models.

    Communication model
    -------------------
    ``CallbackGroup``s (``OnDirty``, ``RequestFocus``, plus any subclass-defined groups) are
    **exclusively** a VM → View communication channel. Every other direction uses direct method
    calls:

    - View → VM: direct method call. The notable view-driven entry points are ``notify_focused()`` and
      ``notify_blurred()``, which views must call from their Textual ``on_focus`` / ``on_blur`` handlers.
    - VM → VM: direct method call on the other VM. If a parent VM needs a sibling to update, it calls
      a public method on that sibling; the sibling emits *its own* events toward the view.
    - VM → View: the view subscribes a callback to a VM's CallbackGroup; the VM emits, the view reacts.

    This is why there is no ``OnBlurred`` callback — the VM has no one to broadcast "I was blurred"
    to. Blur is always view-initiated; the view sees the Textual event and calls ``notify_blurred()``
    to give the VM a chance to react locally. There is no ``request_blur`` either: if one VM wants
    another unfocused, it requests focus elsewhere.

    Standard callbacks
    ------------------
    - ``Callbacks.OnDirty`` — "something changed; please repaint." Fired by mutators on this VM;
      subscribers are typically view-side ``_refresh`` methods.
    - ``Callbacks.RequestFocus`` — "please give this VM (its widget) focus." Fired by
      ``request_focus()``; the canonical subscriber is the view's ``Widget.focus()``.

    Naming convention
    -----------------
    - ``On<Event>`` for after-the-fact observations (``OnDirty``, ``OnSelectionChanged``).
    - ``Request<Action>`` for imperative VM → View directives (``RequestFocus``).
    - ``notify_<event>`` / ``request_<action>`` as method names for the corresponding inbound
      (View → VM) and outbound (VM → View) entry points.

    Subclasses extend ``Callbacks`` via standard class inheritance and register their groups in
    one shot through ``make_callback_groups`` — the dict-spec values document the payload shape:

        class TopicTreeModel(ViewModelBase):
            class Callbacks(ViewModelBase.Callbacks):
                OnSelectionChanged = "OnSelectionChanged"
                OnTopicDeleted     = "OnTopicDeleted"

            def __init__(self):
                super().__init__()
                self.make_callback_groups({
                    self.Callbacks.OnSelectionChanged: None,       # nullary
                    self.Callbacks.OnTopicDeleted:     int,        # one int arg
                })

    Callbacks are reached by key (``self.Callbacks.OnFoo``) at emit / subscribe sites — no
    ``self._foo`` attribute storage. Subclasses that want typed access can layer a ``@property``
    over ``self._callbacks[self.Callbacks.OnFoo]`` to recover the ``CallbackGroup[P]`` annotation.

    Subscription lifetime
    ---------------------
    Subscribers are held by weak reference, so a forgotten unsubscribe no longer leaks the
    subscriber. ``ViewBase.on_unmount`` still unsubscribes explicitly — that's for eager teardown
    (preventing a callback fire between unmount and GC), not for leak prevention.

    Subscriber requirement: the callable must be weak-referenceable, and something other than the
    subscription itself must hold it alive. Bound methods of long-lived objects (the common case)
    satisfy both trivially. A bare lambda passed to ``subscribe`` will die immediately because
    nothing else holds it; if a closure is the right shape, store it on ``self`` first.

    Exception isolation
    -------------------
    Callbacks dispatch inside try/except. A raising subscriber is logged via ``logger.exception``
    and skipped; the remaining subscribers for that group still fire. This applies to all three
    dispatch paths: ``self.emit``, ``Emitter.emit`` fall-through, and ``Emitter._flush``.

    Emitters are single-VM
    ----------------------
    The ``Emitter`` yielded by ``emit_once`` is tied to the VM that created it. Passing a CallbackGroup
    owned by another VM raises ``ValueError``. Cross-VM batching is not a pattern in this codebase —
    each VM emits its own events through its own methods, and any apparent "atomicity" between sibling
    VM updates is provided by the framework's render-frame coalescing, not by our emit machinery.

    Emitter threading convention
    ----------------------------
    Methods on a VM that may participate in *that same VM's* ``emit_once`` batch take an
    ``emitter: Emitter | None = None`` parameter; ``None`` means "fire directly via ``self``" (since
    ``ViewModelBase.emit`` is signature-compatible with ``Emitter.emit``).

    Notably, ``notify_focused`` and ``notify_blurred`` do **not** take an emitter. They are only ever
    called from a view's Textual event handler (which is outside any emit_once chain) — there is no
    legitimate VM caller, so threading an emitter would be meaningless.

    Async boundary
    --------------
    Emitters batch a single *synchronous* chain of execution and never cross task spawns.
    ``asyncio.create_task(...)`` is the stopping point — a spawned coroutine starts a fresh emit
    context and opens its own ``emit_once`` if it needs to coalesce. Async callbacks fired from timers
    should call ``self.emit(...)`` directly, never thread a captured emitter across the boundary.
    """

    class Callbacks:
        """Callback-group keys for this VM and its subclasses. A plain class (not ``Enum``) so
        subclasses can extend the namespace via standard inheritance — ``self.Callbacks.OnDirty``
        resolves on every subclass, alongside whatever each subclass adds:

            class TopicTreeModel(ViewModelBase):
                class Callbacks(ViewModelBase.Callbacks):
                    OnSelectionChanged = "OnSelectionChanged"
                    ...

        Values are strings matching their attribute name. Equality and hashing are string-based,
        so different VM classes that happen to use the same key string collide cleanly.
        """
        OnDirty = "OnDirty"
        RequestFocus = "RequestFocus"

    # Whether this VM is part of the chat pane's ctrl+up/ctrl+down navigation rotation. Default False;
    # feed VMs that present an interactive surface (interrupts, branch indicators) flip this to True
    # in their __init__, and back to False when they become non-interactive (e.g. on interrupt resolve).
    # TODO: this lives on ``ViewModelBase`` for now because adding a feed-only base class would touch
    # every feed VM at once. Consider splitting ``ViewModelBase`` → ``FeedEntryViewModel`` (houses
    # ``is_navigable``) → concrete VMs once the chat-pane MVVM port stabilizes.
    is_navigable: bool = False


    def __init__(self):
        # Registry of every group created via ``make_callback_group``. Must be initialized first so
        # the standard groups registered below can land in it.
        self._callbacks: dict[Hashable, CallbackGroup[Any]] = {}

        self.make_callback_groups({
            ViewModelBase.Callbacks.OnDirty:      None,
            ViewModelBase.Callbacks.RequestFocus: None,
        })

    def make_callback_group[**P](self, key: Hashable) -> CallbackGroup[P]:
        """Construct a CallbackGroup owned by this VM and register it in ``self._callbacks`` under
        ``key``. Subclasses use this to build their own groups so that ``owner_id`` is set correctly,
        the single-VM emitter check fires on cross-VM misuse, and the group is reachable by key.

        Generic over the callback signature ``P``: annotate the attribute at the call site and the
        type checker narrows ``P`` from the assignment target — e.g.

            self._foo: CallbackGroup[[int, str]] = self.make_callback_group(Callbacks.FOO)

        gives ``self._foo`` the expected ``CallbackGroup[[int, str]]`` type. Omitting the annotation
        leaves ``P`` unsolved; for nullary groups this is harmless, for groups with payloads it
        loses the end-to-end signature check that the rest of the machinery is designed to give you.

        Raises ``ValueError`` on duplicate registration — every ``CallbackGroup`` owned by a VM
        must have a unique key.
        """
        if key in self._callbacks:
            raise ValueError(
                f"CallbackGroup with key {key!r} is already registered on this VM. Each key may "
                "be used at most once per VM instance."
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

        Intended access pattern is by key — ``self.emit(Callbacks.FOO, payload)``,
        ``view.subscribe(Callbacks.FOO, view._refresh)``. Subclasses that want typed attribute
        access can layer ``@property`` helpers over ``self._callbacks[key]`` to recover the
        ``CallbackGroup[P]`` annotation.

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
        ``Callbacks`` enum value works directly without reaching for the attribute.

        No owner_id check is performed here: ``self.emit`` is just an immediate fan-out to
        subscribers, with no key-coalescing semantics that cross-VM groups could corrupt. The
        single-VM enforcement applies to emitters yielded by ``emit_once`` only.
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

        The yielded emitter is tied to *this* VM only — passing a CallbackGroup owned by another VM
        raises ``ValueError``. See the class docstring for the rationale.

        merge_strategy controls behavior when the same group is emitted multiple times with differing
        args within the block:
          - STRICT (default): raise ValueError on conflict
          - LAST: last emit's args win
          - FIRST: first emit's args win
        Identical repeats coalesce silently under all strategies.
        """
        emitter = Emitter(
            model=self,
            groups=groups_or_keys if groups_or_keys else None,
            merge_strategy=merge_strategy,
        )
        try:
            yield emitter
        finally:
            emitter._flush()


    def request_focus(self, emitter: Emitter | None = None) -> None:
        """Request that this VM (and its view) take focus. Emits on ``Callbacks.RequestFocus``; the
        canonical subscriber is the view's ``Widget.focus()``, which causes Textual to focus the
        widget and eventually fire the view's ``on_focus`` → ``VM.notify_focused()``.

        Callable from VM code — typically a parent VM orchestrating children, or any code path that
        wants to direct focus programmatically. Accepts an optional ``emitter`` so the focus emit can
        participate in a caller's ``emit_once`` batch on this same VM (emitters are single-VM scoped).

        Does NOT emit ``OnDirty`` here: the downstream ``Widget.focus() → on_focus → notify_focused``
        chain emits it for us. If the widget is already focused, ``Widget.focus()`` is a no-op and
        ``notify_focused`` isn't called — which is correct, because nothing changed.
        """
        if emitter is None:
            emitter = self
        emitter.emit(self.Callbacks.RequestFocus)


    def notify_focused(self) -> None:
        """View-side notification that this VM's view has received focus.

        Inbound counterpart to ``request_focus``. **Must only be called from the view's Textual
        ``on_focus`` event handler** — never from VM code. Focus events arrive outside any
        ``emit_once`` chain, so this method does not accept an ``emitter`` parameter.

        You MUST NOT call ``self.request_focus()`` from within ``self.notify_focused()`` — that creates
        an infinite loop:

            self.request_focus() -> view.focus() -> view.on_focus()
                                 -> self.notify_focused() -> self.request_focus() -> ...

        Calling a *child* VM's ``request_focus()`` is fine — that is the whole point of the delegation
        pattern. A ParentVM that orchestrates children can, on regaining focus, forward focus to the
        appropriate child:

            ParentView.on_focus()
                -> ParentVM.notify_focused()
                -> ParentVM decides which child should be focused
                -> ChildVM.request_focus()
                -> ChildView.focus()
                (-> ChildView.on_focus() -> ChildVM.notify_focused() -> ...)

        Default impl emits ``Callbacks.OnDirty``. Most VMs need a repaint on focus change (focused-
        region styling, hint changes, etc.); Textual handles purely-CSS focus styling automatically,
        but content changes require a refresh. Override and skip the OnDirty emit if your VM truly
        has no focus-dependent rendering.
        """
        self.emit(self.Callbacks.OnDirty)


    def notify_blurred(self) -> None:
        """View-side notification that this VM's view has lost focus.

        Symmetric to ``notify_focused``: called only from the view's Textual ``on_blur`` handler, never
        from VM code. There is no ``request_blur`` (and no ``OnBlurred`` callback) because blur is
        always view-initiated — if some VM wants this one unfocused, it requests focus elsewhere.

        Default impl emits ``Callbacks.OnDirty`` for the same reasons as ``notify_focused``. Override
        and skip the emit if your VM has no blur-dependent rendering.
        """
        self.emit(self.Callbacks.OnDirty)
