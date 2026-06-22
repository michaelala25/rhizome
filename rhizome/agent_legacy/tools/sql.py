"""General-purpose SQL tool for database exploration and modification.

This is a last-resort tool — the agent should always prefer native tools
(list_topics, list_knowledge_entries, read_knowledge_entries, etc.) for standard operations.
Schema introspection is handled by the ``database_schema`` guide.
"""

from __future__ import annotations

import re

from langchain.tools import tool
from langgraph.types import interrupt
from sqlalchemy import text

from rhizome.agent_legacy.tools.visibility import ToolVisibility, tool_visibility
from rhizome.logs import get_logger

_logger = get_logger("agent.sql_tools")

_READ_KEYWORDS = frozenset({"SELECT", "PRAGMA", "EXPLAIN", "WITH"})
_WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE"})


def _first_keyword(sql: str) -> str:
    """Extract the first SQL keyword (uppercased) from a statement."""
    stripped = sql.strip()
    # Skip leading comments
    while stripped.startswith("--") or stripped.startswith("/*"):
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            stripped = stripped[newline + 1:].strip() if newline != -1 else ""
        elif stripped.startswith("/*"):
            end = stripped.find("*/")
            stripped = stripped[end + 2:].strip() if end != -1 else ""
    match = re.match(r"[A-Za-z]+", stripped)
    return match.group(0).upper() if match else ""


def _preview_delete(sql: str) -> str | None:
    """Rewrite a DELETE statement to a SELECT for preview."""
    pattern = re.compile(r"DELETE\s+FROM\b", re.IGNORECASE)
    match = pattern.match(sql.strip())
    if not match:
        return None
    rewritten = pattern.sub("SELECT * FROM", sql.strip(), count=1)
    rewritten = rewritten.rstrip("; \t\n")
    # Append LIMIT if not already present
    if not re.search(r"\bLIMIT\b", rewritten, re.IGNORECASE):
        rewritten += " LIMIT 50"
    return rewritten


def _preview_update(sql: str) -> str | None:
    """Rewrite an UPDATE statement to a SELECT for preview."""
    # Pattern: UPDATE <table> SET ... [WHERE ...]
    match = re.match(
        r"UPDATE\s+(\S+)\s+SET\s+.+?(WHERE\s+.+)?$",
        sql.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    table = match.group(1)
    where = match.group(2) or ""
    rewritten = f"SELECT * FROM {table} {where}".strip().rstrip("; \t\n")
    if not re.search(r"\bLIMIT\b", rewritten, re.IGNORECASE):
        rewritten += " LIMIT 50"
    return rewritten


def _format_rows(columns: list[str], rows: list[list]) -> str:
    """Format rows as a pipe-delimited table."""
    if not columns:
        return "(no columns)"
    lines = [" | ".join(str(c) for c in columns)]
    lines.append("-+-".join("-" * max(len(str(c)), 3) for c in columns))
    for row in rows:
        lines.append(" | ".join(str(v) for v in row))
    return "\n".join(lines)


def build_sql_tools(session_factory) -> dict:
    """Build SQL exploration/modification tools closed over session_factory.

    Returns a dict of tool-name -> tool-function, following the
    ``build_review_tools`` pattern.
    """

    @tool("execute_sql", description=(
        "Execute a SQL statement and return the results. "
        "By default, only read-only statements (SELECT, PRAGMA, EXPLAIN, WITH) "
        "are allowed. Set read_only=False to allow modifications (INSERT, UPDATE, "
        "DELETE) — these require explicit user approval via a confirmation dialog. "
        "Returns up to 200 rows for read queries. "
        "IMPORTANT: Load the 'database_schema' guide first if you are unsure of "
        "table names, column names, or data types. "
        "This is a last-resort tool — prefer native tools (list_topics, "
        "list_knowledge_entries, read_knowledge_entries, create_topics, "
        "delete_topics, etc.) for standard operations."
    ))
    @tool_visibility(ToolVisibility.DEFAULT)
    async def execute_sql_tool(sql: str, read_only: bool = True) -> str:
        keyword = _first_keyword(sql)

        if read_only:
            if keyword not in _READ_KEYWORDS:
                return (
                    f"Rejected: first keyword '{keyword}' is not allowed in read-only mode. "
                    f"Only {', '.join(sorted(_READ_KEYWORDS))} are permitted. "
                    f"Set read_only=False to run modifications."
                )
            try:
                async with session_factory() as session:
                    result = await session.execute(text(sql))
                    columns = list(result.keys()) if result.returns_rows else []
                    if not columns:
                        return "(query returned no columns)"
                    rows = [list(row) for row in result.fetchmany(201)]
                    truncated = len(rows) > 200
                    if truncated:
                        rows = rows[:200]
                    output = _format_rows(columns, rows)
                    if truncated:
                        output += "\n... (results truncated at 200 rows)"
                    return output
            except Exception as exc:
                return f"SQL error: {exc}"

        # -- Modification path (read_only=False) --
        if keyword not in _WRITE_KEYWORDS:
            return (
                f"Rejected: first keyword '{keyword}' is not allowed. "
                f"Only {', '.join(sorted(_WRITE_KEYWORDS))} are permitted for modifications. "
                f"Use read_only=True (default) for SELECT/PRAGMA/EXPLAIN."
            )

        # Build preview for UPDATE/DELETE
        preview_columns: list[str] = []
        preview_rows: list[list] = []
        row_count: int | None = None

        if keyword in ("UPDATE", "DELETE"):
            rewrite_fn = _preview_delete if keyword == "DELETE" else _preview_update
            preview_sql = rewrite_fn(sql)
            if preview_sql:
                try:
                    async with session_factory() as session:
                        count_result = await session.execute(text(preview_sql))
                        all_rows = count_result.fetchall()
                        row_count = len(all_rows)
                        preview_columns = list(count_result.keys()) if count_result.returns_rows else []
                        preview_rows = [list(r) for r in all_rows[:50]]
                except Exception as exc:
                    _logger.warning("Preview query failed: %s", exc)
                    preview_columns = []
                    preview_rows = []
                    row_count = None

        # Interrupt for user confirmation
        result = interrupt({
            "type": "sql_confirmation",
            "sql": sql,
            "preview": {
                "columns": preview_columns,
                "rows": preview_rows,
            },
            "row_count": row_count,
        })

        if result != "Approve":
            return f"User denied SQL modification: {result}"

        try:
            async with session_factory() as session:
                exec_result = await session.execute(text(sql))
                rowcount = exec_result.rowcount
                await session.commit()
            return f"SQL executed successfully. Rows affected: {rowcount}"
        except Exception as exc:
            return f"SQL error: {exc}"

    return {
        "execute_sql": execute_sql_tool,
    }
