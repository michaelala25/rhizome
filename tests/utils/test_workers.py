"""WorkerSchedulerService: the scheduler-resolving holder, exercised through the DI container.

The service is no longer the scheduler callable itself -- it's a holder that *resolves* one, carrying
a mutable binding the presenting view sets on mount and clears on unmount. Headless callers never
bind, so resolution falls back to ``asyncio.create_task``.
"""

import asyncio

import pytest

from rhizome.utils.services import ServiceAccessor
from rhizome.utils.workers import WorkerHandle, WorkerSchedulerService


class BackgroundJob:
    """A consumer that schedules work through an injected scheduler service."""

    def __init__(self, *, scheduler: WorkerSchedulerService):
        self._service = scheduler
        self.done = asyncio.Event()

    def start(self) -> WorkerHandle:
        return self._service.get_scheduler()(self._run())

    async def _run(self):
        self.done.set()


async def test_unbound_holder_falls_back_to_create_task_and_injects():
    services = ServiceAccessor()
    services.register(WorkerSchedulerService, WorkerSchedulerService())
    services.register_descriptor(BackgroundJob)

    job = services.get(BackgroundJob)
    handle = job.start()                          # nothing bound -> create_task fallback
    await asyncio.wait_for(job.done.wait(), timeout=1.0)
    assert hasattr(handle, "cancel")              # satisfies the WorkerHandle contract


async def test_bound_scheduler_is_used_over_the_fallback():
    holder = WorkerSchedulerService()
    seen: list = []

    def scheduler(work):
        seen.append(work)
        return asyncio.ensure_future(work)

    holder.bind(scheduler)
    handle = holder.get_scheduler()(asyncio.sleep(0))
    assert len(seen) == 1                          # the bound scheduler ran, not create_task
    await handle


async def test_unbind_is_compare_and_clear():
    holder = WorkerSchedulerService()

    def first(work):
        return asyncio.create_task(work)

    def second(work):
        return asyncio.create_task(work)

    holder.bind(first)
    holder.unbind(second)                          # a stale unmount: not the current binding...
    assert holder.get_scheduler() is first         # ...so it must NOT clear

    holder.unbind(first)                           # the live view releases its own scheduler
    assert holder.get_scheduler() is asyncio.create_task


def test_root_holder_is_not_bindable():
    root = WorkerSchedulerService(bindable=False)
    assert root.get_scheduler() is asyncio.create_task   # still resolves a scheduler
    with pytest.raises(RuntimeError):
        root.bind(lambda work: asyncio.create_task(work))


def test_child_scope_shadows_the_non_bindable_root():
    root = ServiceAccessor()
    root.register(WorkerSchedulerService, WorkerSchedulerService(bindable=False))

    scope = root.child()
    scope.register(WorkerSchedulerService, WorkerSchedulerService())   # bindable, scoped

    def run_worker(work):
        return asyncio.ensure_future(work)

    scope.get(WorkerSchedulerService).bind(run_worker)                 # resolves the child's holder
    assert scope.get(WorkerSchedulerService).get_scheduler() is run_worker
    assert root.get(WorkerSchedulerService).get_scheduler() is asyncio.create_task  # root untouched


async def test_scheduled_worker_is_cancelable():
    holder = WorkerSchedulerService()
    holder.bind(asyncio.create_task)

    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    handle = holder.get_scheduler()(slow())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle
