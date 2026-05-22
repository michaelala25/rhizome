"""BrowserPaneViewModel — abstract base for tabbed panes in the browser widget.

Each concrete pane (knowledge entries, flashcards, reviews, ...) owns its own
data, sort/filter state, and rendering. The base class nails down the parts
that every pane needs to share with the orchestrator:

  * a stable ``title`` for the tab bar
  * a single ``set_filter(topic_ids)`` entry point the orchestrator calls when
    the topic-tree selection changes
  * a cancel-on-supersede policy so fast multi-selection on the tree doesn't
    leave stale fetches racing each other
  * an ``is_loading`` flag the view can mirror

Subclasses implement ``_fetch()`` — an async coroutine that reads from
``self._filter_ids`` (and any subclass-specific filter state), writes results
to subclass-owned attributes, and emits ``dirty`` as it sees fit. The base
class wraps every ``_fetch`` call in a task whose identity it tracks; only
the most recently-spawned task gets to flip ``is_loading`` back off when it
finishes, so superseded tasks no-op even if they manage to return before the
new one starts.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from typing import Any, Iterable

from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase

_logger = get_logger("browser.pane")


class BrowserPaneViewModel(ViewModelBase):
    """Abstract pane VM. Concrete subclasses must override ``_fetch`` and
    ``title``, plus expose whatever data attributes the corresponding view
    needs to render.

    Filter semantics
    ----------------
    ``set_filter(topic_ids)`` accepts:
      * ``None`` — "no topic filter" (show everything). This is the boot state
        and the state after the user clears their tree selection.
      * a (possibly empty) iterable of topic IDs — the *already-expanded*
        union of subtrees from the user's tree selection. An empty iterable
        means "no rows match" (selection was made but is empty after
        expansion, which is a legal terminal state).

    The orchestrator (``BrowserViewModel``) handles subtree expansion before
    calling here, so panes never run the CTE themselves.

    Cancellation
    ------------
    ``set_filter`` is synchronous (it's called from sync ``SELECTION_CHANGED``
    subscribers) and spawns the real work in a background task. If a fetch is
    already in flight when ``set_filter`` is called again, the previous task
    is cancelled before the new one starts. The cancellation is cooperative:
    the in-flight ``_fetch`` will surface ``asyncio.CancelledError`` from
    whatever await point it was sitting on (typically the DB call), which is
    fine — by then ``_filter_ids`` has been overwritten, so any state the
    cancelled fetch would have committed is already stale.
    """

    # Subclasses override.
    TITLE: str = "<untitled pane>"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        # ``None`` ≠ empty iterable; see filter semantics above.
        self._filter_ids: frozenset[int] | None = None
        # ``True`` once ``set_filter`` has been called at least once. Used by
        # ``set_filter`` to distinguish "filter is already None" from "filter
        # has never been applied" — the first set_filter must fetch even when
        # the requested filter happens to equal the default.
        self._filter_applied: bool = False
        self._is_loading: bool = False
        self._current_task: asyncio.Task[None] | None = None

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

        Idempotent: calling with the same filter the pane already holds is a
        no-op (no cancel, no fetch, no dirty emit). This matters under lazy
        propagation in the orchestrator — switching to a pane that's already
        showing data for the current filter should be instant, not paint a
        loading flash.

        Coalescing across rapid distinct filters is provided by cancellation
        in ``_request_fetch``, not by debouncing here (panes shouldn't need
        to know about input cadence).
        """
        new_filter: frozenset[int] | None = (
            None if topic_ids is None else frozenset(topic_ids)
        )
        if self._filter_applied and new_filter == self._filter_ids:
            return
        self._filter_ids = new_filter
        self._filter_applied = True
        self._request_fetch()

    def _request_fetch(self) -> None:
        """Cancel any in-flight fetch and spawn a fresh one.

        Subclasses call this after mutating their own sort/search/etc. state
        to trigger a refetch with the new parameters. The orchestrator drives
        the same machinery via ``set_filter``.

        The cancelled task's ``finally`` block sees it's no longer the
        current task (because we're about to overwrite ``_current_task``
        synchronously) and quietly bows out without touching state.
        """
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()

        self._is_loading = True
        self.emit(self.dirty)

        self._current_task = asyncio.create_task(self._run_fetch())

    async def _run_fetch(self) -> None:
        """Wrap ``_fetch`` with task-identity guards and ``is_loading`` bookkeeping."""
        my_task = asyncio.current_task()
        try:
            await self._fetch()
        except asyncio.CancelledError:
            # We were superseded by a later set_filter. Don't touch state —
            # the successor has already overwritten ``_filter_ids`` and
            # re-emitted dirty, and a fresh task is about to do the work.
            raise
        except Exception:
            _logger.exception(
                "%s._fetch raised; pane will remain in error state until next set_filter",
                type(self).__name__,
            )
        finally:
            # Only the *current* task — i.e. not one that was cancelled and
            # superseded — is allowed to flip loading off. This is the whole
            # point of stamping a task token.
            if my_task is self._current_task:
                self._is_loading = False
                self.emit(self.dirty)

    @abstractmethod
    async def _fetch(self) -> None:
        """Perform the actual data load for the current filter state.

        Concrete implementations read ``self._filter_ids`` (and any subclass-
        owned filter/sort/search state), do their DB work via
        ``self._session_factory``, and write results into subclass-owned
        attributes. They may emit ``dirty`` as often as they like for
        progressive rendering — the base class will emit a final ``dirty``
        after this coroutine returns regardless.

        Implementations don't need to handle ``asyncio.CancelledError``
        specially; letting it propagate out of the await point is the right
        behavior. The base class catches it.
        """
        raise NotImplementedError
