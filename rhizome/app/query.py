"""Async query primitives with debounce, latest-wins gating, and optional LRU caching.

Two drivers cover the patterns that recur across the view-model layer:

  * ``Query[P, T]``      — a single-result async fetch.
  * ``PagedQuery[P, T]`` — a windowed result set extended page-by-page on demand.

Both share the same lifecycle: a mutator hands in fresh params, the driver debounces, runs the
DB work, and exposes ``state`` / ``result`` (or ``current``) for the view to render. A successor
submit during the debounce window cancels the prior task; once the fetch is awaited the task runs
to completion and a superseded result is discarded by fetch-id.

Caching is opt-in per driver: pass ``cache_key`` to enable, leave it ``None`` to refetch every
time. When enabled, ``cache_max_size`` bounds the cache with an LRU policy.

Cancellation boundary
---------------------
Only the debounce ``asyncio.sleep`` is cancellable. Cancelling a SQLAlchemy async session
mid-query trips a fragile cleanup path in the aiosqlite dialect that surfaces ``CancelledError``
out of ``_finalize_fairy`` and trashes the host application — so once a fetch is in flight it
runs to completion and we discard the result via fetch-id.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from enum import Enum
from typing import Awaitable, Callable, Generic, Hashable, TypeVar

from rhizome.logs import get_logger

_logger = get_logger("query")

P = TypeVar("P")
T = TypeVar("T")
V = TypeVar("V")

DEFAULT_PAGE_LIMIT = 500
DEFAULT_CACHE_MAX_SIZE = 32


# ========================================================================================================
# CACHE
# ========================================================================================================

class _Miss:
    """Singleton sentinel for cache misses — use ``is _MISS`` to test."""

    __slots__ = ()


_MISS = _Miss()


class _LRUCache(Generic[V]):
    """Bounded LRU keyed by hashable params. ``max_size <= 0`` disables caching entirely."""

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._items: OrderedDict[Hashable, V] = OrderedDict()

    def get(self, key: Hashable) -> V | _Miss:
        if self._max_size <= 0:
            return _MISS
        value = self._items.get(key, _MISS)
        if value is not _MISS:
            self._items.move_to_end(key)
        return value

    def put(self, key: Hashable, value: V) -> None:
        if self._max_size <= 0:
            return
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)

    def invalidate(self, predicate: Callable[[Hashable], bool] | None = None) -> None:
        if predicate is None:
            self._items.clear()
            return
        for key in [k for k in self._items if predicate(k)]:
            del self._items[key]


# ========================================================================================================
# QUERY (single result)
# ========================================================================================================

class QueryState(Enum):
    IDLE    = "idle"
    LOADING = "loading"
    SLOW    = "slow"
    ERROR   = "error"
    READY   = "ready"


class Query(Generic[P, T]):
    """Drives a single-result async fetch with debounce, latest-wins gating, and optional caching.

    The owning VM calls ``submit(params)`` on input changes and reads ``.result`` / ``.state``,
    repainting on ``on_change``. A cache hit installs the result synchronously and skips both
    debounce and fetch entirely.
    """

    def __init__(
        self,
        fetch: Callable[[P], Awaitable[T]],
        *,
        cache_key: Callable[[P], Hashable] | None = None,
        cache_max_size: int = DEFAULT_CACHE_MAX_SIZE,
        on_change: Callable[[], None] = lambda: None,
        debounce: float = 0.05,
        slow_after: float = 0.3,
    ) -> None:
        self._fetch       = fetch
        self._cache_key   = cache_key
        self._on_change   = on_change
        self._debounce    = debounce
        self._slow_after  = slow_after

        self.state:  QueryState  = QueryState.IDLE
        self.result: T | None    = None

        self._cache: _LRUCache[T] = _LRUCache(cache_max_size if cache_key is not None else 0)
        self._fetch_id:       int                       = 0
        self._task:           asyncio.Task[None] | None = None
        self._in_debounce:    bool                      = False

    # ----------------------------------------------------------------------------------------------
    # Public surface
    # ----------------------------------------------------------------------------------------------

    def submit(self, params: P) -> None:
        """Request a fetch for ``params``. Cache hit installs synchronously; otherwise debounce →
        fetch, with a successor submit during debounce cancelling the prior task."""
        cached = self._peek(params)
        if cached is not _MISS:
            self.result = cached    # type: ignore[assignment]
            self._set_state(QueryState.READY)
            return

        my_id = self._bump_and_cancel_in_debounce()
        self._in_debounce = True
        self._set_state(QueryState.LOADING)
        self._task = asyncio.create_task(self._run(params, my_id))

    def invalidate(self, predicate: Callable[[Hashable], bool] | None = None) -> None:
        """Drop cache entries — all, or those matching ``predicate``. No-op if caching is off."""
        self._cache.invalidate(predicate)

    # ----------------------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------------------

    def _peek(self, params: P) -> T | _Miss:
        if self._cache_key is None:
            return _MISS
        return self._cache.get(self._cache_key(params))

    def _set_state(self, state: QueryState) -> None:
        self.state = state
        self._on_change()

    def _bump_and_cancel_in_debounce(self) -> int:
        self._fetch_id += 1
        if self._task is not None and not self._task.done() and self._in_debounce:
            self._task.cancel()
        return self._fetch_id

    async def _run(self, params: P, my_id: int) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        if my_id != self._fetch_id:
            return
        self._in_debounce = False

        loop = asyncio.get_running_loop()
        slow = loop.call_later(
            self._slow_after,
            lambda: my_id == self._fetch_id and self._set_state(QueryState.SLOW),
        )
        try:
            result = await self._fetch(params)
        except Exception:
            slow.cancel()
            _logger.exception("Query fetch raised")
            if my_id == self._fetch_id:
                self._set_state(QueryState.ERROR)
            return
        finally:
            slow.cancel()

        # A completed fetch is a valid cache entry even if the task is no longer current.
        if self._cache_key is not None:
            self._cache.put(self._cache_key(params), result)

        if my_id != self._fetch_id:
            return
        self.result = result
        self._set_state(QueryState.READY)


# ========================================================================================================
# PAGED QUERY (windowed result set)
# ========================================================================================================

class PagedList(Generic[T]):
    """A lazily-extended window over one ordered result set. Owns its rows + total, dedups concurrent
    page loads, and gates appends on ``still_current`` (the page fetch itself is uncancellable)."""

    def __init__(
        self,
        fetch_page: Callable[[int, int], Awaitable[list[T]]],
        *,
        page_size: int,
        total: int | None = None,
    ) -> None:
        self._fetch_page = fetch_page
        self._page_size  = page_size
        self.rows:  list[T]   = []
        self.total: int | None = total
        self._loading = False

    @property
    def has_more(self) -> bool:
        return self.total is None or len(self.rows) < self.total

    async def load_more(self, *, still_current: Callable[[], bool] = lambda: True) -> bool:
        if self._loading or not self.has_more:
            return False
        self._loading = True
        try:
            page = await self._fetch_page(len(self.rows), self._page_size)
        finally:
            self._loading = False
        if not still_current():
            return False
        self.rows.extend(page)
        if len(page) < self._page_size:
            self.total = len(self.rows)
        return True


class PagedQuery(Generic[P, T]):
    """Driver for a windowed result set. ``set_params`` establishes — or restores from cache — the
    result set for a param snapshot, with the same debounce + latest-wins gating as ``Query``.
    ``load_more`` extends the current window with no debounce and its own staleness gate.

    Cache is two-level: ``key(params) -> PagedList``, so re-selecting a prior param set restores
    the window WITH its already-loaded pages — pagination state survives navigation.
    """

    def __init__(
        self,
        *,
        fetch_page: Callable[[P, int, int], Awaitable[list[T]]],
        count: Callable[[P], Awaitable[int]],
        cache_key: Callable[[P], Hashable] | None = None,
        cache_max_size: int = DEFAULT_CACHE_MAX_SIZE,
        page_size: int = DEFAULT_PAGE_LIMIT,
        on_change: Callable[[], None] = lambda: None,
        debounce: float = 0.05,
    ) -> None:
        self._fetch_page = fetch_page
        self._count      = count
        self._cache_key  = cache_key
        self._page_size  = page_size
        self._on_change  = on_change
        self._debounce   = debounce

        self.current: PagedList[T] | None = None
        self.state:   QueryState          = QueryState.IDLE

        self._cache: _LRUCache[PagedList[T]] = _LRUCache(
            cache_max_size if cache_key is not None else 0
        )
        self._fetch_id:    int                       = 0
        self._task:        asyncio.Task[None] | None = None
        self._in_debounce: bool                      = False

    # ----------------------------------------------------------------------------------------------
    # Public surface
    # ----------------------------------------------------------------------------------------------

    def set_params(self, params: P) -> None:
        """Switch to the result set for ``params``. Cache hit restores the cached window (rows +
        total + paging cursor) synchronously."""
        cached = self._peek(params)
        if cached is not _MISS:
            self.current = cached   # type: ignore[assignment]
            self._set_state(QueryState.READY)
            return

        my_id = self._bump_and_cancel_in_debounce()
        self._in_debounce = True
        self._set_state(QueryState.LOADING)
        self._task = asyncio.create_task(self._run(params, my_id))

    async def load_more(self) -> bool:
        """Extend the current window by one page. Returns True iff rows were appended."""
        if self.current is None:
            return False
        my_id = self._fetch_id
        changed = await self.current.load_more(still_current=lambda: my_id == self._fetch_id)
        if changed:
            self._on_change()
        return changed

    def invalidate(self, predicate: Callable[[Hashable], bool] | None = None) -> None:
        """Drop cached windows — all, or those matching ``predicate``. No-op if caching is off."""
        self._cache.invalidate(predicate)

    # ----------------------------------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------------------------------

    def _peek(self, params: P) -> PagedList[T] | _Miss:
        if self._cache_key is None:
            return _MISS
        return self._cache.get(self._cache_key(params))

    def _set_state(self, state: QueryState) -> None:
        self.state = state
        self._on_change()

    def _bump_and_cancel_in_debounce(self) -> int:
        self._fetch_id += 1
        if self._task is not None and not self._task.done() and self._in_debounce:
            self._task.cancel()
        return self._fetch_id

    async def _run(self, params: P, my_id: int) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        if my_id != self._fetch_id:
            return
        self._in_debounce = False

        try:
            page0 = await self._fetch_page(params, 0, self._page_size)
            total = await self._count(params)
        except Exception:
            _logger.exception("PagedQuery fetch raised")
            if my_id == self._fetch_id:
                self._set_state(QueryState.ERROR)
            return
        if my_id != self._fetch_id:
            return

        # ``params`` is captured by the lambda so the bound PagedList keeps fetching the right
        # result set even after ``self``'s current params have moved on.
        bound = params
        paged: PagedList[T] = PagedList(
            fetch_page=lambda offset, limit: self._fetch_page(bound, offset, limit),
            page_size=self._page_size,
            total=total,
        )
        paged.rows = list(page0)
        if len(page0) >= total:
            paged.total = len(page0)

        self.current = paged
        if self._cache_key is not None:
            self._cache.put(self._cache_key(params), paged)
        self._set_state(QueryState.READY)
