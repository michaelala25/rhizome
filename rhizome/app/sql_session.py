"""Notifying session factory wrapper for TUI data-refresh integration."""

from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker


class NotifyingSessionFactory:
    """Drop-in wrapper for ``async_sessionmaker`` that fires a callback after commits.

    Usage is identical to ``async_sessionmaker``::

        async with factory() as session:
            ...
            await session.commit()

    When ``session.commit()`` is called and the ``async with`` block exits
    without an exception, *on_commit* is invoked once (regardless of how many
    times ``commit()`` was called within the block).  The callback receives
    a ``frozenset[str]`` of table names that had inserts, updates, or deletes
    in the committed transaction.
    """

    def __init__(
        self,
        factory: async_sessionmaker,
        on_commit: Callable[[frozenset[str]], None],
    ) -> None:
        self._factory = factory
        self._on_commit = on_commit

    def __call__(self) -> "_NotifyingSessionContext":
        return _NotifyingSessionContext(self._factory, self._on_commit)


class _NotifyingSessionContext:
    """Async context manager that tracks commits and fires a callback on clean exit."""

    __slots__ = ("_factory", "_on_commit", "_session", "_committed", "_changed_tables")

    def __init__(self, factory: async_sessionmaker, on_commit: Callable[[frozenset[str]], None]) -> None:
        self._factory = factory
        self._on_commit = on_commit
        self._session = None
        self._committed = False
        self._changed_tables: set[str] = set()

    async def __aenter__(self):
        self._committed = False
        self._changed_tables = set()
        self._session = self._factory()
        original_commit = self._session.commit

        # Listen for flush events on the underlying sync session to capture
        # affected tables.  new/dirty/deleted are populated at flush time
        # but empty by the time commit() is called (flush clears them).
        changed = self._changed_tables

        @event.listens_for(self._session.sync_session, "after_flush")
        def _capture_tables(session, flush_context):
            for obj in session.new | session.dirty | session.deleted:
                table = getattr(obj.__class__, "__tablename__", None)
                if table:
                    changed.add(table)

        async def _tracked_commit():
            await original_commit()
            self._committed = True

        self._session.commit = _tracked_commit  # type: ignore[method-assign]
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._session.close()
        if self._committed and exc_type is None:
            self._on_commit(frozenset(self._changed_tables))
        return False
