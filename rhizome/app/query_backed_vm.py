"""QueryBackedViewModel — base for view-models whose state mirrors a re-runnable async query.

The contract: the VM owns "inputs" (filters, search, cursors, ...) that map to a database query;
a snapshot of those inputs feeds an async ``_fetch``; the result is applied back to local state
by ``_process_fetched_data``. Any mutator that should re-evaluate the query calls
``_request_fetch``, which handles debounce, cancel-on-supersede, and fetch-id staleness gating
so callers don't have to reason about overlapping in-flight work.

Fetch lifecycle
---------------
Each spawned task runs through two phases:

  1. **Debounce** — ``asyncio.sleep(DEBOUNCE_SECONDS)``. Cancellable: a new ``_request_fetch``
     arriving in this window cancels the prior task (safe — nothing's holding a DB connection
     yet) and spawns a fresh one. Bursts of input collapse into a single eventual query.
  2. **Query** — the subclass's ``_fetch`` runs. NOT cancellable: cancelling a SQLAlchemy async
     session mid-query trips a fragile cleanup path in the aiosqlite dialect that surfaces
     ``CancelledError`` out of ``_finalize_fairy`` and trashes the host application. A new
     ``_request_fetch`` arriving in this phase lets the in-flight query finish; its result is
     discarded because the captured fetch id no longer matches the latest.

Subclass contract
-----------------
Two methods are overridden:

  * ``async _fetch() -> Any`` — a **stateless** query. The subclass snapshots whatever VM state
    the query needs synchronously at the top (convention: a ``_query_kwargs()`` helper), runs
    the DB work, and returns a value ``_process_fetched_data`` knows how to consume. ``_fetch``
    must not write to VM state — that's ``_process_fetched_data``'s job. The return type is
    subclass-defined; commonly a small tuple or dict carrying rows and a total count.
  * ``_process_fetched_data(result) -> None`` — applies the result to VM state. Called by the
    base only when the task that produced ``result`` is still the current one; stale orphaned
    tasks never reach this. A final ``dirty`` is emitted after the call returns, so
    implementations generally don't need to.

The fetch-id machinery that decides "still current?" lives entirely on the base; subclasses
participating only in the main fetch path never touch ``_fetch_id`` or ``_still_current``.
Ad-hoc append operations (``load_more`` patterns) that bypass ``_request_fetch`` are the
exception: they capture ``self._fetch_id`` synchronously at the top and consult
``_still_current(my_id)`` before applying, so a concurrent supersede doesn't leave the new
window extended with stale tail rows.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from typing import Any, Callable

from rhizome.logs import get_logger

from .vm import ViewModelBase

_logger = get_logger("query_backed_vm")


class QueryBackedViewModel(ViewModelBase):
    """Abstract VM whose state is the projection of a re-runnable async query.

    Subclasses override ``_fetch`` and ``_process_fetched_data``, expose whatever data
    attributes the view needs to render, and call ``_request_fetch`` from mutators that should
    re-evaluate the query. See the module docstring for the fetch lifecycle and subclass
    contract."""

    # Length of the cancellable debounce window before a fetch's actual DB work begins. Sized to
    # absorb bursts of input (filter toggles, multi-select tree changes, fast cursor scrolling)
    # without being long enough to feel laggy on a single deliberate change. Subclasses can
    # override if their input cadence warrants a different setting.
    DEBOUNCE_SECONDS: float = 0.05

    def __init__(self) -> None:
        super().__init__()
        self._is_loading: bool = False
        # Monotonic fetch id stamped onto each spawned task. The base class only commits a task's
        # result to state when ``self._fetch_id`` still equals the id captured at task start;
        # orphaned tasks bow out silently. Bumped synchronously inside ``_request_fetch`` so
        # successor tasks immediately invalidate prior ones even before the prior ones get
        # scheduled.
        self._fetch_id: int = 0
        # The most recently spawned task. Other (orphaned) tasks may still be running in their
        # query phase, but only this one is tracked for cancellation purposes.
        self._current_task: asyncio.Task[None] | None = None
        # True while ``_current_task`` is still in its debounce ``asyncio.sleep`` (i.e.
        # cancellation is safe). Flipped to False synchronously at the transition into the query
        # phase. ``_request_fetch`` consults this to decide whether to cancel-and-respawn or
        # orphan-and-spawn.
        self._current_in_debounce: bool = False

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    # ------------------------------------------------------------------
    # Fetch machinery
    # ------------------------------------------------------------------

    def _still_current(self, my_id: int) -> bool:
        """True iff the captured fetch id still matches the latest. Used internally to gate
        ``_process_fetched_data``; subclasses generally don't need it, but ad-hoc append
        operations (``load_more`` patterns) use it to gate their own writes against a concurrent
        supersede."""
        return my_id == self._fetch_id

    def _request_fetch(self, on_complete: Callable[[], None] | None = None) -> None:
        """Bumps the fetch id and (re)schedules a debounced fetch.

        Prior tasks are handled in two ways:

        - **In debounce**: cancelled. Cancelling an ``asyncio.sleep`` is safe — nothing's
          holding a DB connection yet.
        - **In query phase** (or already done): orphaned. Cancelling a task parked inside a
          SQLAlchemy session is unsafe (see module docstring). The task runs to completion; its
          result is discarded because the captured fetch id no longer matches.

        Order matters: ``_fetch_id`` is bumped *before* cancelling, so the cancelled task's
        cleanup observes the new id immediately.

        ``on_complete`` (optional) runs synchronously right after ``_process_fetched_data``
        succeeds and before the final ``dirty`` emit — useful for post-refetch bookkeeping (e.g.
        intersecting a selection set with the surviving window after a bulk edit). Not invoked
        if the task is superseded or ``_fetch`` raises.
        """
        self._fetch_id += 1
        new_id = self._fetch_id

        if (
            self._current_task is not None
            and not self._current_task.done()
            and self._current_in_debounce
        ):
            self._current_task.cancel()

        # The task we're about to spawn will start in debounce. Set the flag synchronously so a
        # subsequent ``_request_fetch`` in the same event-loop tick (before the new task actually
        # starts running) still sees the correct state.
        self._current_in_debounce = True
        self._is_loading = True
        self.emit(self.dirty)

        self._current_task = asyncio.create_task(self._debounced_fetch(new_id, on_complete))

    async def _debounced_fetch(
        self,
        my_id: int,
        on_complete: Callable[[], None] | None,
    ) -> None:
        """Runs a fetch with a debounce window in front. See module docstring for the two-phase
        lifecycle.

        ``_current_in_debounce`` is flipped at the debounce → query transition only when the
        task is still current — otherwise a successor has already claimed the flag and writing
        to it here would corrupt its bookkeeping.
        """
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # Superseded during the cancellable debounce window. The successor has already bumped
            # ``_fetch_id`` and set ``_current_in_debounce`` for itself; nothing for us to do.
            return

        # Defensive: there are no awaits between ``_request_fetch`` returning and this point being
        # reached without the cancellation above firing, so a successor shouldn't be able to sneak
        # in here. Check anyway.
        if not self._still_current(my_id):
            return
        self._current_in_debounce = False

        try:
            result = await self._fetch()
        except Exception:
            _logger.exception(
                "%s._fetch raised; VM will remain in error state until next _request_fetch",
                type(self).__name__,
            )
            if self._still_current(my_id):
                self._is_loading = False
                self.emit(self.dirty)
            return

        if not self._still_current(my_id):
            return

        self._is_loading = False
        self._process_fetched_data(result)
        if on_complete is not None:
            on_complete()
        self.emit(self.dirty)

    @abstractmethod
    async def _fetch(self) -> Any:
        """Stateless query. Reads whatever subclass state the query needs synchronously (before
        any await), runs the DB work, and returns a result for ``_process_fetched_data`` to
        consume. Doesn't write to subclass state — that's ``_process_fetched_data``'s job. The
        return type is subclass-defined; commonly a small tuple or dict carrying rows and a
        total count.

        Staleness is handled by the base class: a task superseded mid-flight has its return
        value thrown away without ``_process_fetched_data`` ever seeing it.
        """
        raise NotImplementedError

    @abstractmethod
    def _process_fetched_data(self, result: Any) -> None:
        """Applies a result returned by ``_fetch`` to subclass state.

        Called by the base only when the task that produced ``result`` is still current —
        implementations don't need to check staleness. A final ``dirty`` is emitted after this
        returns, so implementations generally don't need to.
        """
        raise NotImplementedError
