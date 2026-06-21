from pathlib import Path
from typing import Protocol

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from rhizome.logs import get_logger

_logger = get_logger("db")

# Path to alembic.ini, resolved relative to this file's location.
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def get_engine(db_path: str | Path = "rhizome.db") -> AsyncEngine:
    """Create an async SQLite engine pointing at *db_path*.

    Registers a ``connect`` event listener that enables SQLite foreign key
    enforcement (``PRAGMA foreign_keys = ON``) on every new DBAPI connection.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    _logger.info("Engine created for %s (foreign_keys=ON)", db_path)
    return engine


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Return a session factory bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


# ==========================================================================================
# Service: SessionFactoryService
#   Shape : protocol (structural)
#   Scope : root
#   Notes : satisfied by SQLAlchemy's ``async_sessionmaker`` and the ``NotifyingSessionFactory``
#           wrapper -- both are plain callables, so the contract stays structural.
# ==========================================================================================


class SessionFactoryService(Protocol):
    """SSOT async DB session factory -- ``async with sessions() as session:``.

    The production value is the ``async_sessionmaker`` returned by ``get_session_factory``, which
    doubles as this service's DI descriptor (its ``engine`` parameter is injected by type). Any
    callable yielding an ``AsyncSession`` context manager satisfies the protocol, so consumers depend
    on this name rather than on SQLAlchemy's ``async_sessionmaker`` directly.
    """

    def __call__(self) -> AsyncSession: ...


# ==========================================================================================
# Service: ReadOnlySessionFactoryService
#   Shape : protocol (structural)
#   Scope : root
#   Notes : structurally identical to SessionFactoryService, but a distinct name so DI injects a
#           *different* instance -- one bound to a read-only engine. Used by the SQL escape-hatch tool
#           (``agent_new.tools.sql``), whose read-only factory is built by ``read_only_session_factory``.
# ==========================================================================================


class ReadOnlySessionFactoryService(Protocol):
    """Async DB session factory bound to a read-only engine -- the read-only counterpart of
    ``SessionFactoryService``. Same structural contract (``async with sessions() as session:``); the
    separate name is what lets DI hand a consumer the read-only instance rather than the read-write one."""

    def __call__(self) -> AsyncSession: ...


def run_migrations(db_path: str | Path = "rhizome.db") -> None:
    """Run all pending Alembic migrations against *db_path*.

    Safe to call repeatedly — if the database is already at the latest
    revision, this is a no-op.  On a brand-new database, this creates
    all tables from the initial migration.
    """
    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
    )
    command.upgrade(alembic_cfg, "head")
    _logger.info("Migrations applied (db=%s)", db_path)


def init_db(db_path: str | Path = "rhizome.db") -> AsyncEngine:
    """Run migrations and return a production engine with FK enforcement ON.

    Intended for app startup.  Applies any pending Alembic migrations
    (creating all tables on a fresh database), then returns an async
    engine with ``PRAGMA foreign_keys = ON``.
    """
    run_migrations(db_path)
    engine = get_engine(db_path)
    _logger.info("Database initialized (db=%s)", db_path)
    return engine
