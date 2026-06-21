"""Read-only SQL escape hatch: ``execute_sql`` over the full database, for reads the structured tools
can't express (multi-table joins, grouping beyond ``aggregate``, or tables ``query`` doesn't expose).

Gating is belt-and-suspenders, and deliberately *not* string-based — keyword sniffing on the statement is
trivially outwitted (``WITH x AS (...) INSERT ...`` starts with ``WITH``). Both layers are structural:

1. **mode=ro engine** — the database file is opened read-only at the OS level, so writes are impossible by
   construction even if layer 2 has a bug.
2. **SQLite authorizer** — registered per connection, it is invoked by SQLite's *parser* during statement
   compilation and votes on every semantic action (read a column, insert, run a pragma, attach a db, ...).
   We allow a closed set of read actions and DENY everything else, so writes/pragmas/ATTACH are rejected at
   prepare time (nothing partially executes). It also returns ``SQLITE_IGNORE`` for binary-blob columns,
   substituting NULL — keeping 23MB embedding dumps out of the agent's context window.

The aiosqlite wiring has one sharp edge: ``aiosqlite.Connection.set_authorizer`` is a *coroutine* (it
marshals into aiosqlite's worker thread), so calling it from a sync ``connect`` listener silently no-ops.
We reach the underlying stdlib ``sqlite3.Connection`` and call its sync ``set_authorizer`` instead — and let
an attribute miss raise rather than swallow it, so we never run believing the authorizer is installed when
it isn't.
"""

import sqlite3
from pathlib import Path

from langchain.tools import tool
from sqlalchemy import LargeBinary, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from rhizome.db.models import Base

from .visibility import ToolVisibility, tool_visibility

ROW_CAP = 200

# Action codes the agent may perform; every other code (INSERT/UPDATE/DELETE, PRAGMA, ATTACH, CREATE, ...)
# is denied. SELECT = the statement; READ = each (table, column) access; FUNCTION = lower()/count()/...;
# RECURSIVE = WITH RECURSIVE; TRANSACTION = the driver's implicit BEGIN.
_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
})


def _redacted_columns() -> frozenset[tuple[str, str]]:
    """``(table, column)`` pairs blanked to NULL on read — every binary blob in the schema (embeddings,
    source bytes). Derived from the models, so a new ``LargeBinary`` column is redacted automatically."""
    return frozenset(
        (table.name, col.name)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, LargeBinary)
    )


def _make_authorizer(redacted: frozenset[tuple[str, str]]):
    def authorize(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_READ and (arg1, arg2) in redacted:
            return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY
    return authorize


def make_read_only_engine(db_path: str | Path) -> AsyncEngine:
    """A dedicated read-only engine for the SQL escape hatch: the file opened ``mode=ro`` with the
    authorizer installed on every connection. No ``foreign_keys`` pragma — there are no writes to enforce
    against, and the authorizer would deny the pragma anyway."""
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{db_path}?mode=ro&uri=true")
    authorizer = _make_authorizer(_redacted_columns())

    @event.listens_for(engine.sync_engine, "connect")
    def _install_authorizer(dbapi_connection, _record):
        # The raw stdlib sqlite3.Connection behind aiosqlite's async wrapper; its set_authorizer is sync.
        dbapi_connection.driver_connection._connection.set_authorizer(authorizer)

    return engine


def read_only_session_factory(db_path: str | Path) -> async_sessionmaker:
    """Session factory bound to a fresh read-only engine — the dependency ``build_sql_tools`` closes over."""
    return async_sessionmaker(make_read_only_engine(db_path), expire_on_commit=False)


def _format_rows(columns: list[str], rows: list[list]) -> str:
    """Pipe-delimited table."""
    if not columns:
        return "(no columns)"
    header = " | ".join(columns)
    rule = "-+-".join("-" * max(len(c), 3) for c in columns)
    body = "\n".join(" | ".join("null" if v is None else str(v) for v in row) for row in rows)
    return "\n".join([header, rule, body]) if body else f"{header}\n{rule}\n(no rows)"


_EXECUTE_SQL_DESC = (
    "Run a read-only SQL query against the full database and return the rows. A last-resort escape hatch "
    "for reads the structured tools can't express — multi-table joins, grouping beyond `aggregate`, or "
    "tables `query` doesn't expose. Prefer `query`/`aggregate` for everyday access. Strictly read-only: "
    "INSERT/UPDATE/DELETE, PRAGMA, ATTACH and DDL are rejected by the database — do writes through "
    "insert/update/delete. Binary columns (e.g. embeddings) read back as NULL. Capped at "
    f"{ROW_CAP} rows. Consult the database_schema reference for table and column names."
)


def build_sql_tools(read_only_sessions) -> dict:
    """Build the read-only ``execute_sql`` tool closed over a read-only session factory (see
    ``read_only_session_factory``)."""

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("execute_sql", description=_EXECUTE_SQL_DESC)
    async def execute_sql_tool(sql: str) -> str:
        try:
            async with read_only_sessions() as session:
                result = await session.execute(text(sql))
                if not result.returns_rows:
                    return "(statement returned no rows)"
                columns = list(result.keys())
                rows = [list(r) for r in result.fetchmany(ROW_CAP + 1)]
                truncated = len(rows) > ROW_CAP
                out = _format_rows(columns, rows[:ROW_CAP])
                return out + (f"\n... (truncated at {ROW_CAP} rows)" if truncated else "")
        except Exception as exc:
            msg = str(exc)
            if "not authorized" in msg or "readonly" in msg.lower():
                return (f"SQL error: {exc}\n"
                        f"(execute_sql is read-only — use the insert/update/delete tools for writes.)")
            return f"SQL error: {exc}"

    return {"execute_sql": execute_sql_tool}
