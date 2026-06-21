"""Agent-facing tools.

The database tools dominate: a Mongo-style filter DSL (``utils``) compiled onto a curated table registry
(``tables``), driving generic ``query``/``aggregate``/``insert``/``update``/``delete`` tools (``database``)
plus a read-only SQL escape hatch (``sql``). The workflow tools build on top: ``review`` (review-session
state machine), ``flashcard_proposal`` and ``commit`` (propose → present → accept workflows, the latter two
delegating to subagents reached through the runtime), ``guide`` (load reference material), and ``app``
(mode switching, user input). ``visibility`` controls which calls surface in the chat display. The per-mode
tool allowlists live with the prompt content (``rhizome.agent_new.prompts.allowlists``), since their output
is a prompt header.
"""

from .visibility import TOOL_VISIBILITY, ToolVisibility, tool_visibility
from .tables import (
    ALLOWED_TABLES,
    DatabaseToolError,
    PendingWrite,
    TableHooks,
    TableSpec,
    render_schema_reference,
)
from .utils import FilterError, coerce_values, compile_filter, compile_order_by
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
from .guide import build_guide_tools
from .review import build_review_tools
from .flashcard_proposal import build_flashcard_proposal_tools
from .commit import build_commit_tools

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
    "build_commit_tools",
    "build_database_tools",
    "build_flashcard_proposal_tools",
    "build_guide_tools",
    "build_review_tools",
    "build_sql_tools",
    "coerce_values",
    "compile_filter",
    "compile_order_by",
    "make_read_only_engine",
    "read_only_session_factory",
    "render_schema_reference",
    "run_aggregate",
    "run_delete",
    "run_insert",
    "run_query",
    "run_update",
    "tool_visibility",
]
