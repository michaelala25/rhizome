"""BrowserPaneViewModel — abstract base for tabbed panes in the browser widget.

Each concrete pane (knowledge entries, flashcards, reviews, ...) owns its own data, sort/filter state, and
rendering. The base class nails down the parts that every pane needs to share with the orchestrator:

  * a stable ``title`` for the tab bar
  * a single ``set_filter(topic_ids)`` entry point the orchestrator calls when the topic-tree selection
    changes
  * a debounced cancel-on-supersede policy so fast multi-selection on the tree doesn't leave stale fetches
    racing each other
  * an ``is_loading`` flag the view can mirror

Fetch lifecycle
---------------
Each spawned fetch task runs through two phases:

  1. **Debounce** — ``asyncio.sleep(DEBOUNCE_SECONDS)``. Cancellable: a new ``_request_fetch`` arriving in
     this window cancels the prior task (safe — nothing's holding a DB connection yet) and spawns a fresh
     one. Bursts of input collapse into a single eventual query.
  2. **Query** — the subclass's ``_fetch`` runs. NOT cancellable: cancelling a SQLAlchemy async session
     mid-query triggers a fragile cleanup path in the aiosqlite dialect that surfaces ``CancelledError``
     out of ``_finalize_fairy`` and trashes the TUI. Instead, a new ``_request_fetch`` arriving here lets
     the in-flight query finish; its result is discarded by the base class because the captured fetch id
     no longer matches.

Subclass contract
-----------------
Subclasses implement two methods:

  * ``async _fetch() -> Any`` — a **stateless** query. Snapshot whatever state the query needs (via the
    convention of ``_query_kwargs()``) synchronously at the top, then run the DB work and return a result
    the subclass's ``_process_fetched_data`` knows how to consume. ``_fetch`` must not write to subclass
    state — that's ``_process_fetched_data``'s job.
  * ``_process_fetched_data(result) -> None`` — synchronously apply the returned result to subclass state.
    Called by the base class only when the task that produced ``result`` is still the current one; never
    by stale orphaned tasks. The base emits ``dirty`` after this returns, so the implementation doesn't
    need to.

The fetch-id machinery that decides "still current?" lives entirely in the base. Subclasses participating
only in the main fetch path never touch ``_fetch_id`` or ``_still_current``. (Subclass ad-hoc append
operations like ``load_more`` are an exception — they need to gate their own writes against the same id;
see the ``KnowledgeEntryBrowserPaneViewModel.load_more`` for the pattern.)

Future direction (not implemented)
----------------------------------
An **adaptive debounce** would extend the debounce window each time a new ``_request_fetch`` arrives while
the prior task is already in query phase (so the cancellation couldn't be honoured). The window grows
until it exceeds the input cadence, at which point every new request lands inside a cancellable debounce
and no wasted queries ever start. Steady-state semantics would shift from "continuous rate-limited
updates" to "update only when the user settles." Worth considering if real workloads start showing wasted
DB work; not worth the complexity (separate debounce/query handles, expansion ceiling, accept/reset
accounting) for the current pattern.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from typing import Any, Callable, Iterable

from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase

_logger = get_logger("browser.pane")


class BrowserPaneViewModel(ViewModelBase):
    """Abstract pane VM. Concrete subclasses must override ``_fetch``, ``_process_fetched_data``, and
    ``title``, plus expose whatever data attributes the corresponding view needs to render.

    Filter semantics
    ----------------
    ``set_filter(topic_ids)`` accepts:
      * ``None`` — "no topic filter" (show everything). This is the boot state and the state after the user
        clears their tree selection.
      * a (possibly empty) iterable of topic IDs — the *already-expanded* union of subtrees from the user's
        tree selection. An empty iterable means "no rows match" (selection was made but is empty after
        expansion, which is a legal terminal state).

    The orchestrator (``BrowserViewModel``) handles subtree expansion before calling here, so panes never
    run the CTE themselves.
    """

    # Subclasses override.
    TITLE: str = "<untitled pane>"

    # Length of the cancellable debounce window before a fetch's actual DB work begins. Sized to absorb
    # bursts of input (filter toggles via spacebar, multi-select tree changes, etc.) without being long
    # enough to feel laggy on a single deliberate change. Subclasses can override if their input cadence
    # warrants a different setting.
    DEBOUNCE_SECONDS: float = 0.05

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        # ``None`` ≠ empty iterable; see filter semantics above.
        self._filter_ids: frozenset[int] | None = None
        # ``True`` once ``set_filter`` has been called at least once. Used by ``set_filter`` to distinguish
        # "filter is already None" from "filter has never been applied" — the first set_filter must fetch
        # even when the requested filter happens to equal the default.
        self._filter_applied: bool = False
        self._is_loading: bool = False
        # Monotonic fetch id stamped onto each spawned task. The base class only commits a task's result
        # to state when ``self._fetch_id`` still equals the id captured at task start; orphaned tasks bow
        # out silently. Bumped synchronously inside ``_request_fetch`` so successor tasks immediately
        # invalidate prior ones even before the prior ones get scheduled.
        self._fetch_id: int = 0
        # The most recently spawned task. Other (orphaned) tasks may still be running in their query
        # phase, but only this one is tracked for cancellation purposes.
        self._current_task: asyncio.Task[None] | None = None
        # True while ``_current_task`` is still in its debounce ``asyncio.sleep`` (i.e. cancellation is
        # safe). Flipped to False synchronously at the transition into the query phase. ``_request_fetch``
        # consults this to decide whether to cancel-and-respawn or orphan-and-spawn.
        self._current_in_debounce: bool = False

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.TITLE

    @property
    def filter_ids(self) -> frozenset[int] | None:
        return self._filter_ids

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    # ------------------------------------------------------------------
    # Orchestrator-facing API
    # ------------------------------------------------------------------

    def set_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Set the active topic filter and (re)fetch if it actually changed.

        Idempotent: calling with the same filter the pane already holds is a no-op (no cancel, no fetch, no
        dirty emit). This matters under lazy propagation in the orchestrator — switching to a pane that's
        already showing data for the current filter should be instant, not paint a loading flash.

        Coalescing across rapid distinct filters is handled by the debounce in ``_request_fetch``; panes
        don't need to know about input cadence.
        """
        new_filter: frozenset[int] | None = (
            None if topic_ids is None else frozenset(topic_ids)
        )
        if self._filter_applied and new_filter == self._filter_ids:
            return
        self._filter_ids = new_filter
        self._filter_applied = True
        self._request_fetch()

    def _still_current(self, my_id: int) -> bool:
        """True iff the captured fetch id still matches the latest. Used internally to gate
        ``_process_fetched_data``; subclasses generally don't need it, but ad-hoc append operations
        (``load_more``) can use it to gate their own writes against a concurrent supersede."""
        return my_id == self._fetch_id

    def _request_fetch(self, on_complete: Callable[[], None] | None = None) -> None:
        """Bump the fetch id and (re)schedule a debounced fetch.

        Two cases for the prior task:

        - **In debounce**: cancel it. Cancelling an ``asyncio.sleep`` is safe — nothing's holding a DB
          connection yet.
        - **In query phase** (or already done): orphan it. We can't safely cancel a task parked inside a
          SQLAlchemy session (see module docstring). It'll run to completion; its result is discarded by
          the base class because the captured fetch id no longer matches.

        Order matters: bump ``_fetch_id`` *before* cancelling, so the cancelled task's cleanup observes
        the new id immediately.

        ``on_complete`` (optional) fires synchronously right after ``_process_fetched_data`` succeeds and
        before the final ``dirty`` emit. Used by callers that need post-refetch bookkeeping (e.g. the
        knowledge-entry pane intersects ``_selected_ids`` with the surviving window after a bulk edit).
        Not invoked if the task is superseded or ``_fetch`` raises.
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
        # subsequent ``_request_fetch`` in the same event-loop tick (before the new task actually starts
        # running) still sees the correct state.
        self._current_in_debounce = True
        self._is_loading = True
        self.emit(self.dirty)

        self._current_task = asyncio.create_task(self._debounced_fetch(new_id, on_complete))

    async def _debounced_fetch(
        self,
        my_id: int,
        on_complete: Callable[[], None] | None,
    ) -> None:
        """Run a fetch with a debounce window in front. See module docstring for the two-phase lifecycle.

        The transition out of debounce flips ``_current_in_debounce`` only if we're still the current
        task — otherwise a successor has already claimed the flag and we'd corrupt its bookkeeping.
        """
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # Superseded during the cancellable debounce window. The successor has already bumped
            # ``_fetch_id`` and set ``_current_in_debounce`` for itself; nothing for us to do.
            return

        # Defensive: there are no awaits between ``_request_fetch`` returning and this point being reached
        # without the cancellation above firing, so a successor shouldn't be able to sneak in here. Check
        # anyway.
        if not self._still_current(my_id):
            return
        self._current_in_debounce = False

        try:
            result = await self._fetch()
        except Exception:
            _logger.exception(
                "%s._fetch raised; pane will remain in error state until next set_filter",
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
        """Stateless query: read whatever subclass state is needed (synchronously, before any await), run
        the DB work, and return a result. Must not write to subclass state — that's
        ``_process_fetched_data``'s job. The return type is subclass-defined; commonly a small tuple or
        dict carrying rows and a total count.

        The base class handles staleness: if this task gets superseded mid-flight, the returned value is
        thrown away without ``_process_fetched_data`` ever seeing it.
        """
        raise NotImplementedError

    @abstractmethod
    def _process_fetched_data(self, result: Any) -> None:
        """Apply a result returned by ``_fetch`` to subclass state.

        Called by the base only when this task is still current — implementations don't need to check
        staleness. A final ``dirty`` is emitted by the base after this returns, so implementations
        generally shouldn't emit ``dirty`` themselves.
        """
        raise NotImplementedError
