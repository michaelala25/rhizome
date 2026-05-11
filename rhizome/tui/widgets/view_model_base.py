from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Hashable


@dataclass(frozen=True)
class CallbackGroup:
    """A named group of callbacks. ``key`` identifies the group for
    coalescing inside ``emit_once`` blocks; ``callbacks`` is the live
    list of listeners fired on emit.

    Two CallbackGroup instances with the same ``key`` are treated as
    the same group for batching purposes, even if their ``callbacks``
    lists are different objects.
    """
    key: Hashable
    callbacks: list[Callable[..., None]] = field(default_factory=list, compare=False, hash=False)


class Emitter:
    """Emitter handed out by ``ViewModelBase.emit_once``. Captures emits
    for matching groups within its scope and replays them once on exit.
    Emits for non-matching groups fall through immediately. Inert after exit."""

    class MergeStrategy(Enum):
        STRICT = "strict"  # raise on conflicting args for the same group
        LAST = "last"      # last emit wins
        FIRST = "first"    # first emit wins

    def __init__(
        self,
        groups: tuple[CallbackGroup, ...] | None = None,
        merge_strategy: MergeStrategy = MergeStrategy.STRICT,
    ) -> None:
        # None means "batch every group". Otherwise, only batch keys in this set.
        self._batched_keys: frozenset[Hashable] | None = (
            None if groups is None else frozenset(g.key for g in groups)
        )
        self._merge_strategy = merge_strategy
        # Keyed by CallbackGroup.key. Stores (group, args, kwargs) of the
        # captured call; behavior on repeat depends on merge_strategy.
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
                "Cannot emit through an Emitter after its "
                "emit_once block has exited."
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
                f"emit_once block received conflicting args for "
                f"CallbackGroup(key={group.key!r}): "
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

    Provides two standard ``CallbackGroup`` observers — ``dirty`` (the "something
    changed" repaint signal) and ``focus`` (request-focus delegation) — plus the
    ``emit`` / ``emit_once`` / ``subscribe`` / ``unsubscribe`` API. Subclasses
    typically define their own ``Callbacks`` enum for VM-specific groups (e.g.
    request observables that carry arguments).

    Emitter threading convention
    ----------------------------
    Methods that may participate in a caller's ``emit_once`` batch take an
    ``emitter: Emitter | None = None`` parameter; ``None`` means "fire directly
    via ``self``" (since this class's ``emit`` is signature-compatible with
    ``Emitter.emit``). This matches the precedent in ``request_focus``.

    Async boundary
    --------------
    Emitters batch a single *synchronous* chain of execution and never cross
    task spawns. ``asyncio.create_task(...)`` is the stopping point — a spawned
    coroutine starts a fresh emit context and opens its own ``emit_once`` if it
    needs to coalesce multiple emits. The same applies to async callbacks fired
    from timers (they should call ``self.emit(...)`` directly, never thread a
    captured caller emitter across the boundary).
    """

    class Callbacks(Enum):
        DIRTY = "dirty"
        FOCUS = "focus"


    def __init__(self):
        self._dirty = CallbackGroup(ViewModelBase.Callbacks.DIRTY, [])
        self._focus = CallbackGroup(ViewModelBase.Callbacks.FOCUS, [])
    
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
        """Fire an emit immediately. Bypasses any active ``emit_once`` blocks
        — those only capture emits routed through their emitter object."""
        for cb in group.callbacks:
            cb(*args, **kwargs)


    def subscribe(self, group: CallbackGroup, callback: Callable[..., None]) -> None:
        """Subscribe ``callback`` to ``group``. Views should use this rather than
        reaching into ``group.callbacks`` directly."""
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
        """Yield an emitter that batches ``emitter.emit(group, ...)`` calls
        for the given groups, firing each at most once on exit.

        If no groups are passed, every emit through the emitter is batched.
        Emits through the emitter for groups not in the list fall through
        immediately. Emits made via ``self.emit(...)`` directly, or through
        a different emitter, are unaffected.

        merge_strategy controls behavior when the same group is emitted
        multiple times with differing args within the block:
          - STRICT (default): raise ValueError on conflict
          - LAST: last emit's args win
          - FIRST: first emit's args win
        Identical repeats coalesce silently under all strategies.
        """
        emitter = Emitter(
            groups=groups if groups else None,
            merge_strategy=merge_strategy,
        )
        try:
            yield emitter
        finally:
            emitter._flush()


    def request_focus(self, emitter: Emitter | None = None):
        """View-model side to request focus - posts to focus callbacks (typically subscribed by
        the view with a simple Widget.focus() callback). Used when business logic or another VM
        needs to request the focus of this VM.
        """
        if emitter is None:
            emitter = self

        emitter.emit(self.focus)


    def notify_focused(self):
        """View-side notification that focus has been acquired - View's "on_focus()" handler is
        required to call VM.notify_focused(), so this method is available to handle the case
        where the view receives focus _first_, rather than through a VM.

        For this reason, you CANNOT call "request_focus()" from within "notify_focused()", as
        this may create an infinite loop:

            VM.request_focus() -> View.focus() -> View.on_focus() -> View.notify_focused()
        
        This method is instead typically used to reconcile view-side focus with VM-side
        orchestration of sub-widgets and focus. For instance if a ParentVM is responsible for
        deciding which ChildVM needs to be focused, and the ParentVM regains focus through
        its View, then "notify_focused()" can be used to delegate focus to the correct child:

        ParentView.on_focus()       <-- happens at the View layer
            -> ParentVM.notify_focused()
            -> ParentVM checks which child is supposed to be focused
            -> ChildVM.request_focus()
            -> ChildView.focus()

            (-> ChildView.on_focus())
            (-> ChildVM.notify_focused())
            (-> ... continues if ChildVM has its own children)
        """
        pass