"""Worker scheduling: the contract for spawning a background worker from a coroutine.

A ``WorkerSchedulerService`` turns a coroutine into a running, cancelable background worker. Two
callables already satisfy it as-is -- ``asyncio.create_task`` (headless / tests) and Textual's
``Widget.run_worker`` (which binds the worker's lifetime to a widget) -- so a host registers whichever
it wants and consumers depend on the protocol instead of threading a scheduler callable down the stack.
"""

from typing import Any, Coroutine, Protocol


class WorkerHandle(Protocol):
    """A cancelable handle on a running worker. Both ``asyncio.Task`` and Textual's ``Worker`` fit."""

    def cancel(self) -> object: ...


class WorkerSchedulerService(Protocol):
    """Spawns a background worker from a coroutine, returning a cancelable handle."""

    def __call__(self, work: Coroutine[Any, Any, Any]) -> WorkerHandle: ...
