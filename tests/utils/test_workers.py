"""WorkerSchedulerService: the scheduling contract, exercised through the DI container."""

import asyncio

import pytest

from rhizome.utils.services import ServiceAccessor
from rhizome.utils.workers import WorkerHandle, WorkerSchedulerService


class BackgroundJob:
    """A consumer that spawns work through an injected scheduler."""

    def __init__(self, *, scheduler: WorkerSchedulerService):
        self._schedule = scheduler
        self.done = asyncio.Event()

    def start(self) -> WorkerHandle:
        return self._schedule(self._run())

    async def _run(self):
        self.done.set()


async def test_create_task_satisfies_scheduler_and_injects():
    services = ServiceAccessor()
    services.register(WorkerSchedulerService, asyncio.create_task)   # headless default
    services.register_descriptor(BackgroundJob)

    job = services.get(BackgroundJob)
    handle = job.start()
    await asyncio.wait_for(job.done.wait(), timeout=1.0)
    assert hasattr(handle, "cancel")   # satisfies the WorkerHandle contract


async def test_scheduled_worker_is_cancelable():
    services = ServiceAccessor()
    services.register(WorkerSchedulerService, asyncio.create_task)

    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    handle = services.get(WorkerSchedulerService)(slow())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle
