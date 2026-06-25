"""SQL-style database tools: ``query`` / ``aggregate`` / ``insert`` / ``update`` / ``delete`` over the
allowed-tables registry, in place of one bespoke tool per operation per entity.

Each tool takes a ``table`` name resolved through ``tables.ALLOWED_TABLES``; ``query``/``update``/``delete``
take a Mongo-style ``filter`` compiled by ``utils.compile_filter``. The registry is the enforcement
boundary: an unlisted table, a disallowed operation, or a write to a non-writable column is rejected with a
self-describing message the agent can act on, before any session work happens.

Conventions:

- *Strings, not exceptions.* Tools return readable strings — including for validation and database errors —
  so a malformed call becomes a retryable tool message rather than a crashed run.
- *Mutations preview their blast radius.* ``update`` and ``delete`` require a non-empty filter and, unless
  called with ``confirm=True``, return the matched count plus a sample of affected rows instead of writing.
  The agent re-issues the identical call with ``confirm=True`` to apply it. ``insert`` writes directly —
  creating rows has no blast radius to preview.
- *Column projection.* ``query`` returns a lean default set of columns (long text truncated); pass
  ``columns`` to select any columns — including ones omitted by default — returned in full.
- *Two render shapes.* A default (scan) query — and a grouped ``aggregate`` — prints a compact table: the
  column header declared once, then one positional JSON-array row each. An explicit-``columns`` query
  prints vertical ``key: value`` blocks instead, so the requested long-text columns render in full and
  readably.
- *Blanks.* In the vertical shape a blank column (``null`` / empty string) is dropped from its row
  (absence reads as "unset"); in the table shape it is a ``null`` slot. A falsy-but-real value (``False``,
  ``0``) is always kept.
"""

import enum
import functools
import json
from datetime import datetime

from langchain.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from sqlalchemy import delete as sa_delete, func, inspect, select, update as sa_update
from sqlalchemy.exc import SQLAlchemyError

from .tables import ALLOWED_TABLES, OPERATIONS, DatabaseToolError, PendingWrite, TableSpec
from .utils import FilterError, coerce_values, compile_filter, compile_order_by
from .visibility import ToolVisibility, tool_visibility

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
PREVIEW_SAMPLE = 20
TRUNCATE_CHARS = 200

_AGG_FUNCS = {"count": func.count, "sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max}


# ========================================================================================================================
# SHARED HELPERS
# ========================================================================================================================


def _resolve(registry: dict[str, TableSpec], table: str, op: str) -> TableSpec:
    spec = registry.get(table)
    if spec is None:
        raise DatabaseToolError(f"Unknown table {table!r}. Allowed tables: {', '.join(registry)}.")
    if not spec.allows(op):
        allowed = ", ".join(o for o in OPERATIONS if spec.allows(o)) or "none"
        raise DatabaseToolError(f"Operation {op!r} is not allowed on {table!r}. Allowed here: {allowed}.")
    return spec


def _require_filter(filter_, op: str) -> None:
    if not isinstance(filter_, dict) or not filter_:
        raise DatabaseToolError(
            f"{op} requires a non-empty filter, so it cannot affect every row by accident. Pass an "
            f"explicit filter (e.g. a primary-key filter to target a single row)."
        )


def _projection(spec: TableSpec, columns: list[str] | None) -> list[str]:
    exposed = spec.column_names()
    if not columns:
        return spec.default_column_names()
    bad = [c for c in columns if c not in exposed]
    if bad:
        raise DatabaseToolError(
            f"{spec.table_name} has no exposed column(s) {bad}. Exposed columns: {', '.join(exposed)}."
        )
    return list(columns)


def _validate_writable(spec: TableSpec, row: dict) -> None:
    writable = spec.writable_columns()
    bad = [k for k in row if k not in writable]
    if bad:
        raise DatabaseToolError(
            f"{spec.table_name}: column(s) {bad} are not writable. Writable columns: "
            f"{', '.join(sorted(writable))}."
        )


def _is_blank(value) -> bool:
    """Whether a value carries nothing worth showing — ``None`` or an empty string. Deliberately *not*
    falsy: ``False`` and ``0`` are real data and are always rendered."""
    return value is None or value == ""


def _render_value(value, *, truncate: bool) -> str:
    if value is None:
        return "null"
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    if truncate and len(text) > TRUNCATE_CHARS:
        return f"{text[:TRUNCATE_CHARS]}… [{len(text)} chars]"
    return text


def _format_rows(columns: list[str], rows, *, truncate: bool) -> str:
    """Render rows (positional tuples aligned with ``columns``) as ``---``-separated ``key: value`` blocks.
    The shape for reading whole records: blank columns (null / empty string) are dropped from a block —
    absence reads as "unset" — and long text renders in full and naturally (see ``_is_blank``)."""
    if not rows:
        return "(no rows)"
    blocks = []
    for row in rows:
        block = "\n".join(f"  {col}: {_render_value(val, truncate=truncate)}"
                          for col, val in zip(columns, row) if not _is_blank(val))
        blocks.append(block or "  (no non-empty columns)")
    return "\n---\n".join(blocks)


def _json_cell(value, *, truncate: bool) -> str:
    """One table cell as a JSON token. Blanks (null / empty string) collapse to ``null``; ``bool`` stays a
    real JSON bool (not a 0/1 int); long text is truncated to a terse char-count marker. JSON quoting makes
    every value unambiguous regardless of commas / newlines / quotes it contains."""
    if _is_blank(value):
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, enum.Enum):
        return json.dumps(str(value.value), ensure_ascii=False)
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    text = str(value)
    if truncate and len(text) > TRUNCATE_CHARS:
        text = f"{text[:TRUNCATE_CHARS]}… [{len(text)} chars]"
    return json.dumps(text, ensure_ascii=False)


def _format_table(columns: list[str], rows, *, truncate: bool) -> str:
    """Render rows as a compact table: the caller prints the column header once, and each row is a single
    positional JSON array aligned to it. The shape for scanning many rows — column names are not repeated,
    and a fixed arity (blanks are ``null`` slots) keeps every row trivially zippable against the header."""
    if not rows:
        return "(no rows)"
    return "\n".join("[" + ", ".join(_json_cell(v, truncate=truncate) for v in row) + "]" for row in rows)


def _approx_tokens(text: str) -> int:
    """A cheap, provider-neutral token estimate — roughly 4 characters per token. Deliberately rough: it
    exists so the agent can gauge an output's context cost and decide whether to paginate or narrow the
    projection, not to bill anything. A pure function of the rendered text, like the rest of the output."""
    return max(1, len(text) // 4)


def _fmt_tokens(n: int) -> str:
    """Human-readable token count: ``920``, ``5.8k``."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _token_note(body: str) -> str:
    """The shared ``~N tokens`` size hint for a rendered tool body — the budget signal both ``query`` and
    ``aggregate`` append so the agent can gauge an output's context cost (see ``_approx_tokens``)."""
    return f"~{_fmt_tokens(_approx_tokens(body))} tokens"


def _identity(model: type, identity: tuple) -> str:
    pk = [c.key for c in inspect(model).primary_key]
    if len(pk) == 1:
        return f"{pk[0]}={identity[0]}"
    return "(" + ", ".join(f"{k}={v}" for k, v in zip(pk, identity)) + ")"


def _metric_expr(spec: TableSpec, metric: str):
    """Compile a metric spec — ``"count"`` or ``"<func>:<column>"`` (e.g. ``"avg:difficulty"``) — into a
    labelled aggregate expression. ``count`` alone is ``COUNT(*)``; the rest require an exposed column."""
    name, _, column = str(metric).partition(":")
    name = name.lower()
    if name not in _AGG_FUNCS:
        raise DatabaseToolError(
            f"Unknown metric {metric!r}. Valid functions: {', '.join(_AGG_FUNCS)} (as '<func>:<column>', "
            f"e.g. 'avg:difficulty'); 'count' on its own counts rows."
        )
    if name == "count" and not column:
        return func.count().label("count")
    if not column:
        raise DatabaseToolError(f"Metric {name!r} needs a column, e.g. '{name}:difficulty'.")
    if column not in spec.column_names():
        raise DatabaseToolError(
            f"{spec.table_name} has no exposed column {column!r} for metric {metric!r}. "
            f"Exposed columns: {', '.join(spec.column_names())}."
        )
    return _AGG_FUNCS[name](getattr(spec.model, column)).label(f"{name}_{column}")


def report_errors(fn):
    """Turn a tool's expected failure modes into its return value: a malformed filter/value, a registry
    rejection, or a database error comes back as a readable string instead of raising. For an agent-facing
    tool that *is* the contract — a bad call becomes a tool message the model can read and correct, not a
    crashed run. Unexpected exceptions still propagate."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (FilterError, DatabaseToolError) as exc:
            return f"Error: {exc}"
        except SQLAlchemyError as exc:
            return f"Database error: {exc}"
    return wrapper


# ========================================================================================================================
# OPERATIONS
# ========================================================================================================================


@report_errors
async def run_query(session_factory, registry, table, *, filter=None, columns=None,
                    order_by=None, limit=DEFAULT_LIMIT, offset=0) -> str:
    spec = _resolve(registry, table, "query")
    model = spec.model
    explicit = bool(columns)            # did the agent project columns itself?
    proj = _projection(spec, columns)
    clause = compile_filter(model, filter or {})

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    stmt = select(*(getattr(model, c) for c in proj)).where(clause)
    if order_by:
        stmt = stmt.order_by(*compile_order_by(model, order_by))
    stmt = stmt.limit(limit).offset(offset)

    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
        total = await session.scalar(select(func.count()).select_from(model).where(clause))

    # An explicit projection is a "read these records" request — render vertical key:value blocks so the
    # requested (often long-text) columns show in full and readably. The default projection is a "scan" —
    # render a compact table whose column header is declared once, here, with the omitted columns named
    # alongside it (disambiguating "omitted" from "null on this row" and pointing at how to fetch them).
    if explicit:
        body = _format_rows(proj, rows, truncate=False)
    else:
        cols_line = f"columns: {', '.join(proj)}"
        omitted = [c for c in spec.column_names() if c not in proj]
        if omitted:
            cols_line += f" (also available via columns=[...]: {', '.join(omitted)})"
        body = f"{cols_line}\n{_format_table(proj, rows, truncate=True)}"

    # Budget hint: how big this output is, and — when the match is truncated — the projected cost of
    # pulling the whole thing (extrapolated from the page actually rendered). Lets the agent decide to
    # paginate, narrow the filter, or project fewer columns *before* flooding context with the rest.
    shown = len(rows)
    header = f"{spec.table_name}: {total} row(s) matched; showing {shown}"
    if offset:
        header += f" from offset {offset}"
    if shown:
        note = _token_note(body)
        if total > offset + shown:
            full = _approx_tokens(body) * total // shown
            note += (f"; full match ≈ {_fmt_tokens(full)} — raise limit/offset, narrow the filter, "
                     f"or project fewer columns")
        header += f" ({note})"
    return f"{header}\n{body}"


@report_errors
async def run_aggregate(session_factory, registry, table, *, filter=None, group_by=None,
                        metrics=None) -> str:
    spec = _resolve(registry, table, "query")
    model = spec.model
    clause = compile_filter(model, filter or {})

    groups = [group_by] if isinstance(group_by, str) else list(group_by or [])
    bad = [c for c in groups if c not in spec.column_names()]
    if bad:
        raise DatabaseToolError(
            f"{spec.table_name} has no exposed column(s) {bad} to group by. "
            f"Exposed columns: {', '.join(spec.column_names())}."
        )
    metrics = list(metrics or ["count"])
    exprs = [_metric_expr(spec, m) for m in metrics]
    group_attrs = [getattr(model, c) for c in groups]

    # select_from(model) anchors the FROM even when the only selected expression is COUNT(*), which
    # otherwise carries no column to infer the table from.
    stmt = select(*group_attrs, *exprs).select_from(model).where(clause)
    if group_attrs:
        stmt = stmt.group_by(*group_attrs).order_by(*group_attrs)
    stmt = stmt.limit(MAX_LIMIT)

    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()

    # No group_by: a single aggregate row over the whole (filtered) table — already minimal, keep inline.
    if not groups:
        values = ", ".join(f"{m}={_render_value(v, truncate=False)}" for m, v in zip(metrics, rows[0]))
        return f"{spec.table_name}: {values}"

    # Grouped: a table — one row per group, columns = group keys then metrics. The same compact columnar
    # shape as a query scan (header declared once, positional JSON rows), so the group/metric labels are
    # not repeated down every row.
    columns = [*groups, *metrics]
    body = f"columns: {', '.join(columns)}\n{_format_table(columns, rows, truncate=False)}"
    capped = " (capped)" if len(rows) == MAX_LIMIT else ""
    header = f"{spec.table_name}: {len(rows)} group(s){capped} ({_token_note(body)})"
    return f"{header}\n{body}"


@report_errors
async def run_insert(session_factory, registry, table, *, values) -> str:
    spec = _resolve(registry, table, "insert")
    model = spec.model
    rows = values if isinstance(values, list) else [values]
    if not rows:
        raise DatabaseToolError("insert requires at least one row of values.")

    coerced_rows = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            raise DatabaseToolError("Each row must be a non-empty object of column: value.")
        row = spec.normalize("insert", row)
        _validate_writable(spec, row)
        coerced_rows.append(coerce_values(model, row))

    async with session_factory() as session:
        await spec.validate(session, PendingWrite("insert", model, rows=coerced_rows))
        instances = [model(**cr) for cr in coerced_rows]
        session.add_all(instances)
        await session.flush()
        identities = [inspect(obj).identity for obj in instances]
        await session.commit()

    return (f"Inserted {len(instances)} row(s) into {spec.table_name}: "
            + ", ".join(_identity(model, ident) for ident in identities))


@report_errors
async def run_update(session_factory, registry, table, *, filter, values, confirm=False) -> str:
    spec = _resolve(registry, table, "update")
    model = spec.model
    _require_filter(filter, "update")
    if not isinstance(values, dict) or not values:
        raise DatabaseToolError("update requires a non-empty values object.")
    values = spec.normalize("update", values)
    _validate_writable(spec, values)
    coerced = coerce_values(model, values)
    clause = compile_filter(model, filter)

    async with session_factory() as session:
        await spec.validate(session, PendingWrite("update", model, rows=[coerced], clause=clause))
        total = await session.scalar(select(func.count()).select_from(model).where(clause))
        sets = ", ".join(f"{k}={_render_value(v, truncate=False)}" for k, v in values.items())

        if not confirm:
            pk = [c.key for c in inspect(model).primary_key]
            proj = list(dict.fromkeys([*pk, *values.keys()]))
            sample = (await session.execute(
                select(*(getattr(model, c) for c in proj)).where(clause).limit(PREVIEW_SAMPLE)
            )).all()
            lines = [
                f"update would set {sets} on {total} row(s) in {spec.table_name}.",
                f"Current values of affected rows (sample of {len(sample)}):",
                _format_rows(proj, sample, truncate=True),
                "Re-call with confirm=true to apply.",
            ]
            if spec.note:
                lines.append(f"Note: {spec.note}")
            return "\n".join(lines)

        result = await session.execute(sa_update(model).where(clause).values(**coerced))
        await session.commit()
        return f"Updated {result.rowcount} row(s) in {spec.table_name} (set {sets})."


@report_errors
async def run_delete(session_factory, registry, table, *, filter, confirm=False) -> str:
    spec = _resolve(registry, table, "delete")
    model = spec.model
    _require_filter(filter, "delete")
    clause = compile_filter(model, filter)

    async with session_factory() as session:
        await spec.validate(session, PendingWrite("delete", model, clause=clause))
        total = await session.scalar(select(func.count()).select_from(model).where(clause))

        if not confirm:
            proj = spec.column_names()
            sample = (await session.execute(
                select(*(getattr(model, c) for c in proj)).where(clause).limit(PREVIEW_SAMPLE)
            )).all()
            lines = [
                f"delete would remove {total} row(s) from {spec.table_name}.",
                f"Affected rows (sample of {len(sample)}):",
                _format_rows(proj, sample, truncate=True),
                "Re-call with confirm=true to apply.",
            ]
            if spec.note:
                lines.append(f"Note: {spec.note}")
            return "\n".join(lines)

        result = await session.execute(sa_delete(model).where(clause))
        await session.commit()
        return f"Deleted {result.rowcount} row(s) from {spec.table_name}."


# ========================================================================================================================
# TOOL BUILDER
# ========================================================================================================================

_QUERY_DESC = (
    "Read rows from a database table. `filter` is a Mongo-style filter object (see the Database section of "
    "the system prompt for tables, columns, and the filter language). A lean default set of columns is "
    "returned with long text truncated; pass `columns` to project any columns — including ones omitted by "
    "default — returned in full. `order_by` takes specs like ['-created_at', 'title'] (leading '-' = "
    f"descending). Results are capped at {MAX_LIMIT} rows."
)
_AGGREGATE_DESC = (
    "Aggregate rows of a table instead of listing them — counts and min/max/avg/sum, optionally grouped. "
    "`filter` narrows the rows (same filter language as query). `group_by` is a column or list of columns. "
    "`metrics` is a list like ['count', 'avg:difficulty', 'max:created_at'] — 'count' alone counts rows; "
    "the default is ['count']. Example: entries per topic via group_by='topic_id'."
)
_INSERT_DESC = (
    "Create one or more rows in a table. `values` is an object of column: value (or a list of such objects "
    "to insert several). Only writable columns may be set. Returns the new rows' primary keys."
)
_UPDATE_DESC = (
    "Update rows matching `filter` by setting the columns in `values`. A non-empty filter is required. "
    "With confirm=false (default) this previews the matched count and a sample of affected rows without "
    "writing; re-issue the same call with confirm=true to apply."
)
_DELETE_DESC = (
    "Delete rows matching `filter`. A non-empty filter is required. With confirm=false (default) this "
    "previews the matched count and a sample of affected rows without deleting; re-issue with confirm=true "
    "to apply. Deletes may cascade per the schema — check the table's Note."
)


def build_database_tools(registry: dict[str, TableSpec] = ALLOWED_TABLES) -> dict:
    """Build the query/insert/update/delete tools over the table registry, following the ``build_*_tools``
    convention (name -> tool). Root-agent tools: each opens DB sessions off the factory on the agent
    context (``runtime.context.session_factory``) at call time, rather than closing over it."""

    # The parameter schemas below are deliberately lean — bare types, no per-field descriptions. The model
    # learns the filter DSL and the metric/order_by conventions from each tool's prose description plus the
    # `render_schema_reference()` block in the system prompt, rather than from duplicating them into every
    # parameter (which would also bloat the wire payload).
    # TODO: if that presentation ever proves too implicit (the model misuses `filter`/`metrics`/`order_by`),
    # annotate the params with Field(description=...) — e.g. `filter: Annotated[dict, Field(description=...)]`
    # — to make the schemas self-describing.

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("query", description=_QUERY_DESC)
    async def query_tool(table: str, runtime: ToolRuntime, filter: dict | None = None,
                         columns: list[str] | None = None, order_by: list[str] | None = None,
                         limit: int = DEFAULT_LIMIT, offset: int = 0) -> str:
        return await run_query(runtime.context.session_factory, registry, table, filter=filter,
                               columns=columns, order_by=order_by, limit=limit, offset=offset)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("aggregate", description=_AGGREGATE_DESC)
    async def aggregate_tool(table: str, runtime: ToolRuntime, filter: dict | None = None,
                             group_by: str | list[str] | None = None,
                             metrics: list[str] | None = None) -> str:
        return await run_aggregate(runtime.context.session_factory, registry, table, filter=filter,
                                   group_by=group_by, metrics=metrics)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("insert", description=_INSERT_DESC)
    async def insert_tool(table: str, values: dict | list[dict], runtime: ToolRuntime) -> str:
        return await run_insert(runtime.context.session_factory, registry, table, values=values)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("update", description=_UPDATE_DESC)
    async def update_tool(table: str, filter: dict, values: dict, runtime: ToolRuntime,
                          confirm: bool = False) -> str:
        return await run_update(runtime.context.session_factory, registry, table, filter=filter,
                                values=values, confirm=confirm)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("delete", description=_DELETE_DESC)
    async def delete_tool(table: str, filter: dict, runtime: ToolRuntime, confirm: bool = False) -> str:
        return await run_delete(runtime.context.session_factory, registry, table, filter=filter,
                                confirm=confirm)

    return {"query": query_tool, "aggregate": aggregate_tool, "insert": insert_tool,
            "update": update_tool, "delete": delete_tool}
