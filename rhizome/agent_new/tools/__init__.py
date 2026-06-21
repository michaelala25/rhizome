"""Agent-facing tools. The database tools dominate: a Mongo-style filter DSL (``utils``) compiled onto a
curated table registry (``tables``), driving generic ``query``/``insert``/``update``/``delete`` tools
(``database``) plus a read-only SQL escape hatch (``sql``). ``app`` holds app-facing interaction tools."""

from .app import build_app_tools
from .database import (
    build_database_tools,
    run_aggregate,
    run_delete,
    run_insert,
    run_query,
    run_update,
)
from .sql import build_sql_tools, make_read_only_engine, read_only_session_factory
from .tables import (
    ALLOWED_TABLES,
    DatabaseToolError,
    PendingWrite,
    TableHooks,
    TableSpec,
    render_schema_reference,
)
from .utils import FilterError, coerce_values, compile_filter, compile_order_by
from .visibility import TOOL_VISIBILITY, ToolVisibility, tool_visibility

__all__ = [
    "ALLOWED_TABLES",
    "DatabaseToolError",
    "FilterError",
    "PendingWrite",
    "TOOL_VISIBILITY",
    "TableHooks",
    "TableSpec",
    "ToolVisibility",
    "build_app_tools",
    "build_database_tools",
    "build_sql_tools",
    "tool_visibility",
    "make_read_only_engine",
    "read_only_session_factory",
    "coerce_values",
    "compile_filter",
    "compile_order_by",
    "render_schema_reference",
    "run_aggregate",
    "run_delete",
    "run_insert",
    "run_query",
    "run_update",
]
