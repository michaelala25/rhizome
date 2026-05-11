from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Hashable


@dataclass(frozen=True)
class CallbackGroup:
    """A named group of callbacks owned by a single ``ViewModelBase`` instance.

    ``key`` identifies the group within its owning VM (typically a value from that VM's ``Callbacks``
    enum). ``owner_id`` is ``id(vm)`` of the VM that owns this group — used by ``Emitter`` to enforce
    single-VM batching. ``callbacks`` is the live list of listeners fired on emit.

    Construct via ``ViewModelBase._make_group(key)`` rather than directly — the helper sets ``owner_id``
    correctly.
    """
    key: Hashable
    owner_id: int
    callbacks: list[Callable[..., None]] = field(default_factory=list, compare=False, hash=False)


class Emitter:
    """Single-VM emitter handed out by ``ViewModelBase.emit_once``. Captures emits for matching groups
    within its scope and replays them once on exit. Emits for non-matching groups fall through
    immediately. Inert after exit.

    Emitters are tied to the VM that created them (via ``owner_id``). Passing a CallbackGroup owned by
    a different VM to ``emitter.emit(...)`` raises ``ValueError`` — by design. Under the project's
    communication model (see ``ViewModelBase``):

    - Each VM owns its own events and emits them through its own methods.
    - Cross-VM coordination uses direct method calls, never shared emitters.
    - If a parent VM needs siblings to repaint, it calls public methods on them; each emits *its own*
      ``dirty`` independently.

    So an emitter never legitimately fires another VM's events — and we fail loudly if you try, rather
    than silently coalescing distinct VMs' groups under the same ``Callbacks`` enum value.
    """

    class MergeStrategy(Enum):
        STRICT = "strict"  # raise on conflicting args for the same group
        LAST = "last"      # last emit wins
        FIRST = "first"    # first emit wins

    def __init__(
        self,
        owner_id: int,
        groups: tuple[CallbackGroup, ...] | None = None,
        merge_strategy: MergeStrategy = MergeStrategy.STRICT,
    ) -> None:
        self._owner_id = owner_id
        # None means "batch every group". Otherwise, only batch keys in this set.
        self._batched_keys: frozenset[Hashable] | None = (
            None if groups is None else frozenset(g.key for g in groups)
        )
        self._merge_strategy = merge_strategy
        # Keyed by CallbackGroup.key. Stores (group, args, kwargs) of the captured call; behavior on
        # repeat depends on merge_strategy.
        self._pending: dict[Hashable, tuple[CallbackGroup, tuple[Any, ...], dict[str, Any]]] = {}
        self._closed = False

    def _is_batched(self, group: CallbackGroup) -> bool:
        return self._batched_keys is None or group.key in self._batched_keys

    def emit(
        self,
        group: CallbackGroup,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if self._closed:
            raise RuntimeError(
                "Cannot emit through an Emitter after its emit_once block has exited."
            )

        if group.owner_id != self._owner_id:
            raise ValueError(
                f"Emitter is owned by VM id={self._owner_id} but received a CallbackGroup owned by "
                f"VM id={group.owner_id}. Emitters are single-VM scoped: cross-VM coordination happens "
                f"through direct method calls, not shared emitters. See ViewModelBase docstring."
            )

        # Non-batched group: fall through immediately.
        if not self._is_batched(group):
            for cb in group.callbacks:
                cb(*args, **kwargs)
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
                for cb in group.callbacks:
                    cb(*args, **kwargs)
        finally:
            self._closed = True
            self._pending.clear()


class ViewModelBase:
    """Base class for MVVM view-models in ``rhizome/tui/widgets/``.

    Communication model
    -------------------
    ``CallbackGroup``s (``dirty``, ``focus``, plus any subclass-defined groups) are **exclusively** a
    VM → View communication channel. Every other direction uses direct method calls:

    - View → VM: direct method call. The notable view-driven entry points are ``notify_focused()`` and
      ``notify_blurred()``, which views must call from their Textual ``on_focus`` / ``on_blur`` handlers.
    - VM → VM: direct method call on the other VM. If a parent VM needs a sibling to update, it calls
      a public method on that sibling; the sibling emits *its own* events toward the view.
    - VM → View: the view subscribes a callback to a VM's CallbackGroup; the VM emits, the view reacts.

    This is why there is no ``blurred`` CallbackGroup — the VM has no one to broadcast "I was blurred"
    to. Blur is always view-initiated; the view sees the Textual event and calls ``notify_blurred()``
    to give the VM a chance to react locally. There is no ``request_blur`` either: if one VM wants
    another unfocused, it requests focus elsewhere.

    Standard CallbackGroups
    -----------------------
    - ``dirty`` — "something changed; please repaint." Fired by mutators on this VM; subscribers are
      typically view-side ``_refresh`` methods.
    - ``focus`` — "please give this VM (its widget) focus." Fired by ``request_focus()``; the canonical
      subscriber is the view's ``Widget.focus()``.

    Subclasses define their own ``Callbacks`` enum for VM-specific groups (e.g. request observables
    that carry arguments via ``emit(group, *args, **kwargs)``), and construct them with
    ``self._make_group(key)`` so ``owner_id`` is set correctly.

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

    class Callbacks(Enum):
        DIRTY = "dirty"
        FOCUS = "focus"


    def __init__(self):
        self._dirty = self._make_group(ViewModelBase.Callbacks.DIRTY)
        self._focus = self._make_group(ViewModelBase.Callbacks.FOCUS)

    def _make_group(self, key: Hashable) -> CallbackGroup:
        """Construct a CallbackGroup owned by this VM. Subclasses use this to build their own
        CallbackGroups so that ``owner_id`` is set correctly and the single-VM emitter check fires on
        cross-VM misuse.
        """
        return CallbackGroup(key=key, owner_id=id(self))

    @property
    def dirty(self):
        return self._dirty

    @property
    def focus(self):
        return self._focus


    def emit(
        self,
        group: CallbackGroup,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Fire an emit immediately. Bypasses any active ``emit_once`` blocks — those only capture
        emits routed through their emitter object.

        No owner_id check is performed here: ``self.emit`` is just an immediate fan-out to subscribers,
        with no key-coalescing semantics that cross-VM groups could corrupt. The single-VM enforcement
        applies to emitters yielded by ``emit_once`` only.
        """
        for cb in group.callbacks:
            cb(*args, **kwargs)


    def subscribe(self, group: CallbackGroup, callback: Callable[..., None]) -> None:
        """Subscribe ``callback`` to ``group``. Views should use this rather than reaching into
        ``group.callbacks`` directly."""
        group.callbacks.append(callback)


    def unsubscribe(self, group: CallbackGroup, callback: Callable[..., None]) -> None:
        """Remove ``callback`` from ``group``. No-op if not subscribed."""
        try:
            group.callbacks.remove(callback)
        except ValueError:
            pass


    @contextmanager
    def emit_once(
        self,
        *groups: CallbackGroup,
        merge_strategy: Emitter.MergeStrategy = Emitter.MergeStrategy.STRICT,
    ):
        """Yield an emitter that batches ``emitter.emit(group, ...)`` calls for the given groups,
        firing each at most once on exit.

        If no groups are passed, every emit through the emitter is batched. Emits through the emitter
        for groups not in the list fall through immediately. Emits made via ``self.emit(...)``
        directly, or through a different emitter, are unaffected.

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
            owner_id=id(self),
            groups=groups if groups else None,
            merge_strategy=merge_strategy,
        )
        try:
            yield emitter
        finally:
            emitter._flush()


    def request_focus(self, emitter: Emitter | None = None) -> None:
        """Request that this VM (and its view) take focus. Emits on the ``focus`` group; the canonical
        subscriber is the view's ``Widget.focus()``, which causes Textual to focus the widget and
        eventually fire the view's ``on_focus`` → ``VM.notify_focused()``.

        Callable from VM code — typically a parent VM orchestrating children, or any code path that
        wants to direct focus programmatically. Accepts an optional ``emitter`` so the focus emit can
        participate in a caller's ``emit_once`` batch on this same VM (emitters are single-VM scoped).

        Does NOT emit ``dirty`` here: the downstream ``Widget.focus() → on_focus → notify_focused``
        chain emits dirty for us. If the widget is already focused, ``Widget.focus()`` is a no-op and
        ``notify_focused`` isn't called — which is correct, because nothing changed.
        """
        if emitter is None:
            emitter = self
        emitter.emit(self.focus)


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

        Default impl emits ``self.dirty``. Most VMs need a repaint on focus change (focused-region
        styling, hint changes, etc.); Textual handles purely-CSS focus styling automatically, but
        content changes require a refresh. Override and skip the dirty emit if your VM truly has no
        focus-dependent rendering.
        """
        self.emit(self.dirty)


    def notify_blurred(self) -> None:
        """View-side notification that this VM's view has lost focus.

        Symmetric to ``notify_focused``: called only from the view's Textual ``on_blur`` handler, never
        from VM code. There is no ``request_blur`` (and no ``blurred`` CallbackGroup) because blur is
        always view-initiated — if some VM wants this one unfocused, it requests focus elsewhere.

        Default impl emits ``self.dirty`` for the same reasons as ``notify_focused``. Override and skip
        the dirty emit if your VM has no blur-dependent rendering.
        """
        self.emit(self.dirty)
