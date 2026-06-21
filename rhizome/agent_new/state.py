"""Custom graph state schema for the root agent.

All proposal and session state types are defined here so the full state
shape is visible in one place.
"""

from __future__ import annotations

from langchain.agents.middleware.types import AgentState as _AgentState

from typing import Annotated, TypedDict

from rhizome.resources_new import ResourceTreeNode

from .base import accumulate_cleanups, CleanupRequest, ConsumedResources


# ---------------------------------------------------------------------------
# Review state
# ---------------------------------------------------------------------------

class ReviewScope(TypedDict):
    """Which topics and entries are in scope for a review session."""
    topic_ids: list[int]
    entry_ids: list[int]


class ReviewConfig(TypedDict):
    """User-selected configuration for a review session."""
    style: str
    """Review style: ``"flashcard"``, ``"conversation"``, or ``"mixed"``."""

    critique_timing: str
    """When to deliver critique: ``"during"`` or ``"after"``."""

    ephemeral: bool
    """If ``True``, the session is not persisted for long-term tracking."""

    user_instructions: str | None
    """Free-form instructions from the user for this review session."""


class ReviewState(TypedDict):
    """State for an active review session.

    Stored in ``RootAgentState.review``.  ``None`` when no review
    session is active.  Lazily initialized by the first call to
    ``review_update_session_state``.
    """

    session_id: int
    """Database session ID.  Always set — ephemeral sessions still get a DB record."""

    scope: ReviewScope | None
    """Selected topics and entries for review."""

    config: ReviewConfig | None
    """User-selected review configuration."""

    flashcard_queue: list[int]
    """Flashcard DB IDs to present, popped as used."""

    entry_coverage: dict[int, int]
    """Map of entry_id to touch count, incremented by ``review_record_interaction``."""

    interaction_count: int
    """Total number of review interactions recorded this session."""

    discussion_plan: str | None
    """Agent-generated plan for conversational review flow."""


def merge_typeddict_field(left: dict | None, right: dict | None) -> dict | None:
    """Generic shallow-merge reducer for nullable ``TypedDict`` agent-state
    fields, so parallel tool calls that touch disjoint top-level keys compose
    cleanly.

    - ``right is None`` → clear the field (LWW).
    - ``left is None`` → adopt ``right`` wholesale (initialization path —
      tools that open a workflow return a full TypedDict here).
    - Both dicts → shallow merge: each key in ``right`` overwrites that key
      in ``left``; keys absent from ``right`` are preserved.

    Conflicts on the same top-level key fall back to LWW. Callers that mutate
    state should return only the keys they changed; returning a full snapshot
    would clobber concurrent updates touching other keys. To clear the field
    use ``None`` — an empty dict ``{}`` is a no-op merge, not a clear.
    """
    if right is None:
        return None
    if left is None:
        return right
    return {**left, **right}


# ---------------------------------------------------------------------------
# Flashcard proposal state
# ---------------------------------------------------------------------------

class FlashcardProposalItem(TypedDict):
    """A single proposed flashcard, stored in agent state."""
    id: int
    topic_id: int
    question_text: str
    answer_text: str
    entry_ids: list[int]
    testing_notes: str | None


class FlashcardProposalState(TypedDict):
    """Consolidated state for the flashcard proposal workflow.

    Stored in ``RootAgentState.flashcard_proposal_state``.
    """
    items: list[FlashcardProposalItem]
    """The staged flashcard items."""


class CommitProposalEntry(TypedDict):
    """A single proposed knowledge entry, stored in agent state."""
    id: int
    title: str
    content: str
    entry_type: str
    topic_id: int


class CommitProposalState(TypedDict):
    """Consolidated state for the commit proposal workflow.

    Stored in ``RootAgentState.commit_proposal_state``.
    """
    payload: list[dict]
    """Selected conversation messages for knowledge commit (``{"index", "content"}``)."""

    proposal: list[CommitProposalEntry]
    """Proposed knowledge entries awaiting user approval."""

    proposal_diff: str | None
    """Human-readable diff summary from the most recent user edit session.
    Written by ``commit_proposal_present`` on Edit; read by
    ``commit_invoke_subagent`` to inform the subagent of user changes."""


class BaseAgentState(_AgentState):
    """Framework-level agent state every conversation has, independent of agent kind — today just the
    message-lifetime request channel. Domain agents extend it (see ``RootAgentState``)."""

    pending_cleanups: Annotated[list[CleanupRequest], accumulate_cleanups]
    """Declarative cleanup requests (``cleanup.CleanupRequest``) filed by tools / app hooks / the agent's
    ``cleanup_context``, drained by the engine's cleanup pass each compile — the engine stays the sole
    emitter of the actual edits. State, not context, so a request raised mid-stream survives a crash.
    Appended via ``accumulate_cleanups``; the pass writes ``None`` to drain."""


class RootAgentState(BaseAgentState):
    """Extended agent state for checkpoint/replay.

    All fields use default last-write-wins semantics.  Nullable fields
    (``review``, ``flashcard_proposal_state``, ``commit_proposal_state``)
    persist in the checkpoint until explicitly cleared by a tool via
    ``Command(update={...})``.  They are NOT reset to ``None`` in
    ``stream()``'s ``next_input``.
    """

    mode: str
    """Active conversation mode: ``"idle"``, ``"learn"``, or ``"review"``. Per-branch state —
    inherited through ``branch`` via checkpoint copy, divergent across siblings afterward.

    The engine-committed *snapshot* of the mode, NOT its source of truth. The live ``AppContextStore``
    on the context (``ctx.app_state``) is the SSOT both the user (view) and the agent (the ``set_mode``
    tool) write through. The prompt engine diffs that store against this field at compile and, on a
    switch, commits the new value here and narrates it (full guide on first entry, concise reminder on
    re-entry, tool allowlist). A tool must never write this field directly — a double write would erase
    the store-vs-state delta the engine reacts to. The system prompt and tool set never change with mode.
    """

    verbosity: str
    """Answer-verbosity preference for this conversation. Per-branch state, inherited through ``branch``
    like ``mode``; set by the app via a ``StateUpdatePayload`` and consumed by the prompt engine when
    shaping ephemeral context."""

    consumed_resource_context: Annotated[ConsumedResources | None, merge_typeddict_field]
    """Per-channel snapshot of the resource descriptions this thread has consumed into context.

    The stores hold *desired* load state; this field records what THIS thread has actually ingested,
    so the per-compile delta is simply desired-vs-consumed (see ``prompt_engine.resource_deltas``).
    Written wholesale by the engine's compile step in the same state update as the content messages
    it produces — consumption becomes a fact the moment the update lands. Living in state, it
    inherits through ``branch`` via checkpoint copy, so children diff against their parent's
    baseline for free. ``None`` until the first consumption."""

    consumed_resource_index: list[ResourceTreeNode] | None
    """Snapshot of the index load-state this thread's "what's queryable" reminder reflects.

    The index store holds the *desired* set (graph-global); this records what THIS thread's reminder
    message currently announces, so the per-compile decision is a plain desired-vs-snapshot set
    comparison (see ``prompt_engine._compile_index``). Written wholesale by compile in the same update
    as the reminder message; inherits through ``branch`` via checkpoint copy. Default LWW — only the
    compile step writes it. ``None`` until first use."""

    review: Annotated[ReviewState | None, merge_typeddict_field]
    """Review session state machine; ``None`` when no review is active.

    Reduced via :func:`merge_typeddict_field` so parallel tool calls that
    touch disjoint top-level keys (e.g. one sets ``scope``, another sets
    ``config``) compose cleanly. Same-key conflicts fall back to LWW."""

    flashcard_proposal_state: Annotated[FlashcardProposalState | None, merge_typeddict_field]
    """Consolidated flashcard proposal state: staged items.
    ``None`` when no proposal is active.

    Reduced via :func:`merge_typeddict_field` — see ``review`` for details."""

    commit_proposal_state: Annotated[CommitProposalState | None, merge_typeddict_field]
    """Consolidated commit proposal state: payload, proposal entries, and
    diff summary.  ``None`` when no commit workflow is active.

    Reduced via :func:`merge_typeddict_field` — see ``review`` for details."""