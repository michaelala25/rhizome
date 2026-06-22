"""Mongo-style filter compilation: JSON filter documents -> SQLAlchemy expressions.

The agent-facing database tools accept MongoDB-style filter documents (the de-facto JSON query syntax)
and compile them onto the ORM models. There is deliberately no parser here — a JSON document already is
the AST — and no query engine — SQLAlchemy is the query engine. This module is the adapter between the
two, plus the validation layer that turns malformed filters into retryable, self-describing errors.

Grammar::

    filter := {field: cond, ...}              -- sibling keys AND together
            | {"$and": [filter, ...]}
            | {"$or":  [filter, ...]}
            | {"$not": filter}

    cond   := <scalar>                        -- equality shorthand; null compiles to IS NULL
            | {op: value, ...}                -- sibling operators AND together
            | filter                          -- relationship fields only: correlated subfilter

    field  := <column> | <relationship> | "<relationship>.<path>"

Dotted paths and nested subfilters quantify through relationships as correlated EXISTS — ``.any()`` for
collections, ``.has()`` for to-one references. Note ``{"entries.id": 1, "entries.title": "X"}`` is two
independent EXISTS (different related rows may satisfy each), while ``{"entries": {"id": 1, "title":
"X"}}`` is one EXISTS whose conditions must hold on the SAME related row.

Operators::

    $eq $ne $gt $gte $lt $lte    comparisons (scalar value)
    $in $nin                     membership (list value)
    $like                        SQL LIKE, explicit % wildcards
    $contains                    case-insensitive substring
    $exists                      {"$exists": bool} -- IS (NOT) NULL on columns, (NOT) EXISTS on relationships
    $in_subtree                  topic-id columns only: membership in the topic subtree rooted at the
                                 given topic id(s), roots inclusive (compiles to a recursive CTE)

Values are coerced from their JSON representations onto the column's python type: ISO-8601 strings to
datetimes (normalized to the column's aware/naive convention), enums by value then by name, numeric
strings to numbers. Coercion failures raise ``FilterError`` like any other validation problem.

NULL handling follows SQL three-valued logic, not MongoDB's missing-field semantics: ``$ne`` and ``$not``
do NOT match rows where the column is NULL. Match NULLs explicitly via ``{"field": null}`` or
``{"field": {"$exists": false}}``.
"""

import enum
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import and_, inspect, not_, or_, select, true
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import TypeDecorator

from rhizome.db.models import Topic


class FilterError(ValueError):
    """A malformed filter document. Messages name the offending field or operator and enumerate the
    valid alternatives, so tools can surface them verbatim and the agent can self-correct."""


_COMPARISON_OPS = {
    "$eq": lambda col, v: col == v,
    "$ne": lambda col, v: col != v,
    "$gt": lambda col, v: col > v,
    "$gte": lambda col, v: col >= v,
    "$lt": lambda col, v: col < v,
    "$lte": lambda col, v: col <= v,
}

_ALL_OPS = (*_COMPARISON_OPS, "$in", "$nin", "$like", "$contains", "$exists", "$in_subtree")


# ========================================================================================================================
# PUBLIC API
# ========================================================================================================================


def compile_filter(model: type, filter_: Mapping[str, Any]) -> ColumnElement[bool]:
    """Compile a filter document into a boolean expression against ``model``'s columns."""
    if not isinstance(filter_, Mapping):
        raise FilterError(f"Filter must be an object, got {type(filter_).__name__}: {filter_!r}")

    clauses: list[ColumnElement[bool]] = []
    for key, value in filter_.items():
        if key in ("$and", "$or"):
            if not isinstance(value, list) or not value:
                raise FilterError(f"{key} expects a non-empty list of filter objects")
            combine = and_ if key == "$and" else or_
            clauses.append(combine(*(compile_filter(model, branch) for branch in value)))
        elif key == "$not":
            if not isinstance(value, Mapping):
                raise FilterError("$not expects a filter object")
            clauses.append(not_(compile_filter(model, value)))
        elif key.startswith("$"):
            raise FilterError(f"Unknown boolean operator {key!r}. Valid: $and, $or, $not")
        else:
            clauses.append(_compile_field(model, key, value))

    return and_(*clauses) if clauses else true()


def compile_order_by(model: type, specs: list[str]) -> list[ColumnElement]:
    """Compile ordering specs like ``["-created_at", "title"]`` (``-`` prefix = descending)."""
    clauses = []
    for spec in specs:
        name = spec.lstrip("+-")
        attr, relationship = _resolve_field(model, name)
        if relationship is not None:
            raise FilterError(f"Cannot order by relationship {name!r} — order by a column instead")
        clauses.append(attr.desc() if spec.startswith("-") else attr.asc())
    return clauses


def coerce_values(model: type, values: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce a ``{column: json_value}`` payload onto ``model``'s column python types — the write-side
    counterpart to ``compile_filter``, used by the insert/update tools. Columns only: relationship and
    dotted-path keys are rejected. Coercion rules and error messages are shared with filter compilation,
    so an enum value, ISO datetime, or numeric string is accepted the same way on both paths."""
    out: dict[str, Any] = {}
    for name, value in values.items():
        attr, relationship = _resolve_field(model, name)
        if relationship is not None:
            raise FilterError(f"{model.__name__}.{name} is a relationship and cannot be set directly")
        out[name] = _coerce(attr, value, f"{model.__name__}.{name}")
    return out


# ========================================================================================================================
# FIELD AND OPERATOR COMPILATION
# ========================================================================================================================


def _resolve_field(model: type, name: str):
    """Resolve a field name to ``(attribute, relationship | None)``, mapper-known attributes only."""
    mapper = inspect(model)
    if name in mapper.relationships:
        return getattr(model, name), mapper.relationships[name]
    if name in mapper.column_attrs:
        return getattr(model, name), None

    valid = sorted([*mapper.column_attrs.keys(), *mapper.relationships.keys()])
    raise FilterError(f"{model.__name__} has no field {name!r}. Valid fields: {', '.join(valid)}")


def _compile_field(model: type, path: str, cond: Any) -> ColumnElement[bool]:
    head, _, rest = path.partition(".")
    attr, relationship = _resolve_field(model, head)

    if relationship is not None:
        quantify = attr.any if relationship.uselist else attr.has
        target = relationship.mapper.class_

        if rest:
            return quantify(_compile_field(target, rest, cond))
        if isinstance(cond, Mapping) and set(cond) == {"$exists"}:
            if not isinstance(cond["$exists"], bool):
                raise FilterError(f"{model.__name__}.{head}: $exists expects true or false")
            return quantify() if cond["$exists"] else not_(quantify())
        if isinstance(cond, Mapping) and not any(k.startswith("$") for k in cond):
            return quantify(compile_filter(target, cond))
        raise FilterError(
            f"{model.__name__}.{head} is a relationship — use a dotted path ({head}.<field>), "
            f"a nested filter object, or {{'$exists': bool}}"
        )

    if rest:
        raise FilterError(f"{model.__name__}.{head} is a column; dotted paths require a relationship")

    ops = cond if isinstance(cond, Mapping) else {"$eq": cond}
    if not ops:
        raise FilterError(f"Empty condition object for {model.__name__}.{head}")
    return and_(*(_compile_op(model, head, attr, op, value) for op, value in ops.items()))


def _compile_op(model: type, field: str, attr, op: str, value: Any) -> ColumnElement[bool]:
    ctx = f"{model.__name__}.{field}"

    if op in _COMPARISON_OPS:
        if value is None and op not in ("$eq", "$ne"):
            raise FilterError(f"{ctx}: {op} cannot compare against null (use $exists)")
        return _COMPARISON_OPS[op](attr, _coerce(attr, value, ctx))

    if op in ("$in", "$nin"):
        if not isinstance(value, list):
            raise FilterError(f"{ctx}: {op} expects a list, got {value!r}")
        coerced = [_coerce(attr, v, ctx) for v in value]
        return attr.in_(coerced) if op == "$in" else attr.not_in(coerced)

    if op in ("$like", "$contains"):
        if not isinstance(value, str):
            raise FilterError(f"{ctx}: {op} expects a string, got {value!r}")
        return attr.like(value) if op == "$like" else attr.icontains(value)

    if op == "$exists":
        if not isinstance(value, bool):
            raise FilterError(f"{ctx}: $exists expects true or false")
        return attr.is_not(None) if value else attr.is_(None)

    if op == "$in_subtree":
        return _in_subtree(attr, value, ctx)

    if not op.startswith("$"):
        raise FilterError(f"{ctx}: condition keys must be operators starting with '$', got {op!r}")
    raise FilterError(f"{ctx}: unknown operator {op!r}. Valid operators: {', '.join(_ALL_OPS)}")


def _in_subtree(attr, value: Any, ctx: str) -> ColumnElement[bool]:
    """Membership in the topic subtree(s) rooted at the given id(s), via a recursive CTE."""
    roots = value if isinstance(value, list) else [value]
    if not roots or not all(isinstance(r, int) and not isinstance(r, bool) for r in roots):
        raise FilterError(f"{ctx}: $in_subtree expects a topic id or non-empty list of topic ids")

    subtree = select(Topic.id).where(Topic.id.in_(roots)).cte(recursive=True)
    subtree = subtree.union_all(select(Topic.id).where(Topic.parent_id == subtree.c.id))
    return attr.in_(select(subtree.c.id))


# ========================================================================================================================
# VALUE COERCION
# ========================================================================================================================


def _coerce(attr, value: Any, ctx: str) -> Any:
    """Coerce a JSON-native value onto the column's python type. ``None`` passes through (SQLAlchemy
    renders ``== None`` as IS NULL)."""
    if value is None:
        return None

    # Unwrap TypeDecorators (e.g. UTCDateTime) so python_type and timezone come from the real impl.
    col_type = attr.type
    if isinstance(col_type, TypeDecorator):
        col_type = col_type.impl
    try:
        pytype = col_type.python_type
    except NotImplementedError:
        return value

    if isinstance(pytype, type) and issubclass(pytype, enum.Enum):
        return _coerce_enum(pytype, value, ctx)
    if pytype is datetime:
        return _coerce_datetime(value, aware=bool(getattr(col_type, "timezone", False)), ctx=ctx)
    if pytype is bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise FilterError(f"{ctx}: expected a boolean, got {value!r}")
    if pytype in (int, float):
        if isinstance(value, bool):
            raise FilterError(f"{ctx}: expected a number, got {value!r}")
        if isinstance(value, (int, float)):
            return pytype(value)
        if isinstance(value, str):
            try:
                return pytype(value)
            except ValueError:
                raise FilterError(f"{ctx}: expected a number, got {value!r}") from None
        raise FilterError(f"{ctx}: expected a number, got {value!r}")
    if pytype is str:
        if isinstance(value, str):
            return value
        raise FilterError(f"{ctx}: expected a string, got {value!r}")

    return value


def _coerce_enum(enum_cls: type[enum.Enum], value: Any, ctx: str) -> enum.Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        pass
    try:
        return enum_cls[value]
    except (KeyError, TypeError):
        valid = ", ".join(str(member.value) for member in enum_cls)
        raise FilterError(f"{ctx}: {value!r} is not a valid {enum_cls.__name__}. Valid: {valid}") from None


def _coerce_datetime(value: Any, *, aware: bool, ctx: str) -> datetime:
    """Parse ISO-8601 strings, then normalize to the column's convention: aware-UTC for timezone
    columns (``UTCDateTime`` rejects naive values on bind), naive-UTC for plain DateTime columns."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise FilterError(f"{ctx}: expected an ISO-8601 datetime, got {value!r}") from None
    if not isinstance(value, datetime):
        raise FilterError(f"{ctx}: expected an ISO-8601 datetime, got {value!r}")

    if aware:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
