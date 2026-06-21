"""Worker scheduling: spawning a cancelable background worker from a coroutine.

A ``WorkerScheduler`` is the callable contract -- coroutine in, cancelable handle out. Two callables
satisfy it as-is: ``asyncio.create_task`` (headless / tests) and Textual's ``Widget.run_worker``
(which binds the worker's lifetime to a widget).

``WorkerSchedulerService`` is the injected service: a *holder* that resolves a ``WorkerScheduler`` and
carries a mutable binding the presenting view installs on mount and clears on unmount -- so a view-model
schedules background work bound to the widget's lifetime without ever importing Textual.
``WorkerSchedulerBinding`` is the first-party implementation. The pattern:

    # scope-owner VM (constructs its own child scope so concurrent presenters don't collide)
    services.register(WorkerSchedulerService, WorkerSchedulerBinding())
    ...
    self.services.get(WorkerSchedulerService).get_scheduler()(coro)   # VM schedules

    # its view
    on_mount:   self._vm.services.get(WorkerSchedulerService).bind(self.run_worker)
    on_unmount: self._vm.services.get(WorkerSchedulerService).unbind(self.run_worker)

The root scope registers a ``bindable=False`` binding as a scope-less fallback: it still resolves a
scheduler (``asyncio.create_task``), but ``bind`` raises -- catching the bug where a view binds and
resolution fell through to root because its VM never opened a scoped service.
"""

import asyncio
from typing import Any, Coroutine, Optional, Protocol


class WorkerHandle(Protocol):
    """A cancelable handle on a running worker. Both ``asyncio.Task`` and Textual's ``Worker`` fit."""

    def cancel(self) -> object: ...


class WorkerScheduler(Protocol):
    """Spawns a background worker from a coroutine, returning a cancelable handle. Satisfied as-is by
    ``asyncio.create_task`` and Textual's ``Widget.run_worker``."""

    def __call__(self, work: Coroutine[Any, Any, Any]) -> WorkerHandle: ...


# ==========================================================================================
# Service: WorkerSchedulerService
#   Shape : protocol + first-party impl (WorkerSchedulerBinding, below)
#   Scope : per-presenter child scope; the root holds a non-bindable fallback
# ==========================================================================================


class WorkerSchedulerService(Protocol):
    """The injected holder: resolves a ``WorkerScheduler`` and carries the binding a view installs.

    View-models schedule via ``get_scheduler()``; the presenting view ``bind``s its ``run_worker`` on
    mount and ``unbind``s on unmount. ``WorkerSchedulerBinding`` is the first-party implementation."""

    __slots__ = ()

    def bind(self, scheduler: WorkerScheduler) -> None: ...
    def unbind(self, scheduler: Optional[WorkerScheduler] = None) -> None: ...
    def get_scheduler(self) -> WorkerScheduler: ...


class WorkerSchedulerBinding(WorkerSchedulerService):
    """Resolves a ``WorkerScheduler``, holding a mutable binding set by the presenting view.

    ``get_scheduler`` returns the bound scheduler, or ``asyncio.create_task`` when nothing is bound
    (headless, or between a view unmounting and the next mounting). A ``bindable=False`` binding is the
    root-scope fallback: it resolves the same way but refuses ``bind`` (see module docstring)."""

    __slots__ = ("_bindable", "_bound")

    def __init__(self, *, bindable: bool = True) -> None:
        self._bindable = bindable
        self._bound: Optional[WorkerScheduler] = None

    def bind(self, scheduler: WorkerScheduler) -> None:
        if not self._bindable:
            raise RuntimeError(
                "Cannot bind the root WorkerSchedulerBinding: a view resolved the shared root scheduler, "
                "which means its view-model never registered a scoped one. Open a child scope "
                "(services.child(name)) and register a WorkerSchedulerBinding (under the "
                "WorkerSchedulerService key) there before binding."
            )
        self._bound = scheduler

    def unbind(self, scheduler: Optional[WorkerScheduler] = None) -> None:
        """Clear the binding. Pass the scheduler you bound so a late unmount (the previous view) can't
        clear a binding the newly-mounted view already installed -- only the current one is cleared."""
        if scheduler is None or scheduler is self._bound:
            self._bound = None

    def get_scheduler(self) -> WorkerScheduler:
        return self._bound if self._bound is not None else asyncio.create_task
