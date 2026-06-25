"""Allowed-tables registry: the curated subset of the schema the database tools operate on.

A ``TableSpec`` names one ORM model and declares the agent's contract with it — which operations are
permitted (query / insert / update / delete), which columns are visible, and which are writable — plus a
one-line description. ``ALLOWED_TABLES`` is the curated set, and it is the single source of truth for two
concerns that must never drift apart:

- *Enforcement* — the database tools (``database.py``) resolve every call through this registry, so an
  unlisted table, a forbidden operation, or a write to a non-writable column is rejected before touching
  the session.
- *Documentation* — ``render_schema_reference`` builds the agent-facing schema guide straight from these
  specs, describing exactly the tables, columns, and relationships the tools actually expose. The agent is
  never shown a column it cannot use.

Visibility is deliberately narrower than the full schema: binary blobs (embeddings, source bytes) and
scheduler internals (FSRS state) are hidden, and tables with their own dedicated workflows (review session
management, resource ingestion) are query-only or absent.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Enum, Text, inspect
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import TypeDecorator

from rhizome.db.models import (
    Flashcard,
    KnowledgeEntry,
    RelatedKnowledgeEntries,
    ReviewSession,
    Tag,
    Topic,
)
from rhizome.db.operations import would_create_cycle

OPERATIONS = ("query", "insert", "update", "delete")
_ALL_OPS = frozenset(OPERATIONS)

# Column keys never offered for writing by default — surrogate keys and server-managed timestamps.
_NEVER_WRITABLE = frozenset({"created_at", "updated_at"})

# Column keys omitted from the default query projection — server-managed timestamps, low signal in a
# scoping read. Still exposed, so an explicit ``columns=[...]`` brings them back. Mirrors _NEVER_WRITABLE.
_NEVER_DEFAULT = frozenset({"created_at", "updated_at"})


# ========================================================================================================================
# ERRORS & WRITE HOOKS
# ========================================================================================================================


class DatabaseToolError(ValueError):
    """A call the registry rejects: unknown table, disallowed operation, non-writable column, missing
    filter, or a write a table's hook guards against. The tools catch it and return it as a message."""


@dataclass(frozen=True)
class PendingWrite:
    """The write a hook is asked to guard, in a shape uniform across operations: ``rows`` carries the
    coerced column values (insert/update), ``clause`` the compiled filter (update/delete)."""

    op: str                                    # "insert" | "update" | "delete"
    model: type
    rows: list[dict] | None = None
    clause: ColumnElement[bool] | None = None


class TableHooks:
    """Optional per-table policy that refines or guards a write *without changing its shape* — the generic
    CRUD engine stays generic. Subclass and override only what's needed; both methods default to no-ops.

    - ``normalize`` — a pure, per-row transform applied before coercion (defaults, canonicalization).
    - ``validate`` — an async guard run inside the transaction before the write lands; raise
      ``DatabaseToolError`` to reject. Receives the open session, so it can check the proposed change
      against live state (e.g. a reachability query). A raise rolls the transaction back untouched.
    """

    def normalize(self, op: str, row: dict) -> dict:
        return row

    async def validate(self, session, write: PendingWrite) -> None:
        return None


class RelatedEntriesHooks(TableHooks):
    """Keep the entry graph acyclic: reject an inserted edge whose target can already reach its source.
    Reuses ``db.operations.would_create_cycle`` — the same predicate ``add_relation`` enforces — so the
    invariant has a single definition rather than a re-implementation behind the generic insert."""

    async def validate(self, session, write: PendingWrite) -> None:
        if write.op != "insert":
            return
        for row in write.rows or []:
            src, tgt = row.get("source_entry_id"), row.get("target_entry_id")
            if src is not None and tgt is not None and await would_create_cycle(session, src, tgt):
                raise DatabaseToolError(
                    f"inserting edge {src} -> {tgt} would create a cycle (the target already reaches the "
                    f"source); the entry graph must stay acyclic"
                )


# ========================================================================================================================
# TABLE SPEC
# ========================================================================================================================


@dataclass(frozen=True)
class TableSpec:
    """The agent's contract with one ORM model. Columns and relationships are read live off the mapper, so
    a schema change is reflected automatically; only the policy (ops, hidden, writable) lives here."""

    model: type
    description: str
    ops: frozenset[str]
    hidden: frozenset[str] = frozenset()
    """Column keys never exposed — kept out of query output and the schema doc, and rejected on write."""
    default_hidden: frozenset[str] = frozenset()
    """Columns exposed but omitted from the default query projection (long text, dead columns). Returned
    only when named explicitly in ``columns=[...]``; server-managed timestamps are dropped globally."""
    writable: frozenset[str] | None = None
    """Insert/update-able columns. ``None`` defaults to every exposed, non-PK, non-timestamp column."""
    name: str | None = None
    """Agent-facing table name; defaults to the model's ``__tablename__``."""
    note: str | None = None
    """A caveat surfaced in the schema doc and in mutation previews (cascades, acyclicity, ...)."""
    hooks: TableHooks | None = None
    """Optional write policy (``normalize`` / ``validate``). ``None`` = the bare generic path."""

    @property
    def table_name(self) -> str:
        return self.name or self.model.__tablename__

    def allows(self, op: str) -> bool:
        return op in self.ops

    def normalize(self, op: str, row: dict) -> dict:
        return self.hooks.normalize(op, row) if self.hooks is not None else row

    async def validate(self, session, write: PendingWrite) -> None:
        if self.hooks is not None:
            await self.hooks.validate(session, write)

    def columns(self) -> list:
        """Exposed ``Column`` objects, in definition order (hidden columns removed)."""
        return [c for c in inspect(self.model).columns if c.key not in self.hidden]

    def column_names(self) -> list[str]:
        return [c.key for c in self.columns()]

    def default_column_names(self) -> list[str]:
        """The projection a query returns when none is named explicitly: exposed columns minus the global
        timestamps and this table's ``default_hidden``. All remain requestable via ``columns=[...]``."""
        omit = _NEVER_DEFAULT | self.default_hidden
        return [c for c in self.column_names() if c not in omit]

    def relationship_names(self) -> list[str]:
        """Exposed relationship keys — usable as filter traversal targets (dotted paths / subfilters)."""
        return [r.key for r in inspect(self.model).relationships if r.key not in self.hidden]

    def writable_columns(self) -> frozenset[str]:
        if self.writable is not None:
            return self.writable
        return frozenset(
            c.key for c in self.columns()
            if not c.primary_key and c.key not in _NEVER_WRITABLE
        )


# ========================================================================================================================
# THE REGISTRY
# ========================================================================================================================

_SPECS: list[TableSpec] = [
    TableSpec(
        Topic,
        "Topics form a tree via the self-referential parent_id (NULL parent = a root). Knowledge entries "
        "attach to a topic at any depth.",
        ops=_ALL_OPS,
        writable=frozenset({"parent_id", "name", "description"}),
        note="Deleting a topic cascades to its subtopics and to every knowledge entry beneath it.",
    ),
    TableSpec(
        KnowledgeEntry,
        "The atomic units of knowledge — a titled fact, exposition, or overview belonging to one topic.",
        ops=_ALL_OPS,
        # content is long text — omitted from the default read and fetched explicitly; difficulty and
        # speed_testable are dead columns pending removal.
        default_hidden=frozenset({"content", "difficulty", "speed_testable"}),
        writable=frozenset(
            {"topic_id", "title", "content", "additional_notes", "entry_type", "difficulty", "speed_testable"}
        ),
    ),
    TableSpec(
        Tag,
        "Free-form labels attached to knowledge entries. Names are lowercased by convention.",
        ops=frozenset({"query", "insert"}),
        writable=frozenset({"name"}),
    ),
    TableSpec(
        RelatedKnowledgeEntries,
        "Directed, typed edges between knowledge entries — the knowledge graph layered over the entries.",
        ops=frozenset({"query", "insert", "delete"}),
        writable=frozenset({"source_entry_id", "target_entry_id", "relationship_type"}),
        note="Edges must stay acyclic; an insert that would create a cycle is rejected.",
        hooks=RelatedEntriesHooks(),
    ),
    TableSpec(
        Flashcard,
        "Spaced-repetition cards. Authoring and FSRS scheduling run through the review workflow; this "
        "exposes them read-only, without the scheduler's internal state.",
        ops=frozenset({"query"}),
        hidden=frozenset({"fsrs_state", "fsrs_step", "stability", "difficulty", "last_review"}),
    ),
    TableSpec(
        ReviewSession,
        "Past and in-progress review sessions. Read for history and grounding (e.g. prior final_summary).",
        ops=frozenset({"query"}),
        hidden=frozenset({"additional_args"}),
    ),
]

ALLOWED_TABLES: dict[str, TableSpec] = {spec.table_name: spec for spec in _SPECS}


# ========================================================================================================================
# SCHEMA REFERENCE RENDERING
# ========================================================================================================================
# Builds the agent-facing database guide from the registry. Intended to be injected into the system prompt
# (the full DSL grammar lives in ``utils`` for the implementation; this is the operator-level summary the
# agent needs to construct calls).

_FILTER_LANGUAGE = """\
## Filter language

`filter` is a JSON object; sibling keys AND together.

    {field: value}                 equality ({field: null} matches IS NULL)
    {field: {op: value, ...}}      operators (siblings AND together)
    {"$and"|"$or": [filter, ...]}  boolean combination
    {"$not": filter}

Operators: $eq $ne $gt $gte $lt $lte | $in $nin (list) | $like (explicit % wildcards) |
$contains (case-insensitive substring) | $exists (true/false) | $in_subtree (topic-id columns: the
subtree rooted at the given topic id(s), roots included).

Relationships are traversed with dotted paths (`topic.name`) or a nested object (`tags: {name: "x"}`);
both compile to EXISTS. NULL follows SQL three-valued logic: $ne / $not do NOT match NULL rows — match
them explicitly with `{field: null}` or `{field: {$exists: false}}`."""


def _render_type(col) -> str:
    """A compact, agent-readable type name for a column."""
    col_type = col.type
    if isinstance(col_type, Enum) and getattr(col_type, "enums", None):
        return f"enum({'|'.join(col_type.enums)})"
    if isinstance(col_type, Text):
        return "text"
    # Unwrap TypeDecorators (e.g. UTCDateTime) so python_type comes from the real impl, not the wrapper.
    if isinstance(col_type, TypeDecorator):
        col_type = col_type.impl
    try:
        py = col_type.python_type
    except (NotImplementedError, AttributeError):
        return type(col_type).__name__.lower()
    return {int: "int", str: "str", float: "float", bool: "bool", datetime: "datetime",
            bytes: "bytes", dict: "json", list: "json"}.get(py, getattr(py, "__name__", "value"))


def _render_column(col) -> str:
    parts = [f"{col.key}: {_render_type(col)}"]
    if col.nullable and not col.primary_key:
        parts[0] += "?"
    flags: list[str] = []
    if col.primary_key:
        flags.append("PK")
    fk = next(iter(col.foreign_keys), None)
    if fk is not None:
        flags.append(f"→ {fk.column.table.name}")
    return parts[0] + (f" ({', '.join(flags)})" if flags else "")


def render_schema_reference(registry: dict[str, TableSpec] = ALLOWED_TABLES) -> str:
    """Render the database schema + query-language reference for the agent's system prompt."""
    out = [
        "# Database",
        "",
        "Interact with the database through five tools — `query`, `aggregate`, `insert`, `update`, "
        "`delete` — over the tables below. Each takes a `table` name; `query`/`aggregate`/`update`/`delete` "
        "take a `filter` (see the filter language). `aggregate` works on any queryable table (counts, "
        "min/max/avg/sum, optional group-by). `update` and `delete` preview their blast radius unless "
        "called with `confirm=true`.",
        "",
        _FILTER_LANGUAGE,
        "",
        "## Tables",
    ]
    for spec in registry.values():
        mapper = inspect(spec.model)
        out.append("")
        out.append(f"### {spec.table_name} — {spec.description}")
        out.append(f"Operations: {', '.join(op for op in OPERATIONS if spec.allows(op))}")

        out.append("Columns:")
        out += [f"  - {_render_column(c)}" for c in spec.columns()]

        rels = [r for r in mapper.relationships if r.key not in spec.hidden]
        if rels:
            out.append("Relationships (for filtering):")
            out += [
                f"  - {r.key} → {r.mapper.class_.__tablename__} ({'many' if r.uselist else 'one'})"
                for r in rels
            ]

        if "insert" in spec.ops or "update" in spec.ops:
            out.append(f"Writable: {', '.join(sorted(spec.writable_columns()))}")
        if spec.note:
            out.append(f"Note: {spec.note}")

    return "\n".join(out)
