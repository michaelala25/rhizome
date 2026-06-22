"""Prompt compilation: the layer that decides what an agent's next model request looks like.

The machinery splits along one line — whether a step's output is allowed to persist in the checkpointer:

- ``PromptEngine.compile`` runs in ``before_model`` and returns a state update. Its output flows through
  the state schema's reducers and lands in the checkpoint: it is a *fact about the conversation*
  (ingested payloads, repair patches, injected guides).
- ``PromptEngine.prepare`` runs in ``wrap_model_call`` and reshapes the outgoing request only. Its output
  is a *view* for a single wire request (message ordering, cache breakpoints) and is never written back.

Two independent axes classify every message the engine manages, and they are the vocabulary the layout
and the lifetime machinery (on ``PromptEngine`` and ``RootPromptEngine``) build from:

- *Lifetime* — how long a message's identity persists in state. ``permanent`` (the default) lives
  forever; ``semi-permanent`` is eligible for later reclamation. Lifetime applies only to messages born
  in ``compile``: a *derived* message — built fresh in ``prepare`` from current state, never persisted —
  has a position but no lifetime by construction (the index reminder is the motivating example). A
  genuine single-run lifetime is conceivable but unbuilt; it would need run-scoped hooks, not the
  per-model-call ones here.
- *Position* — where ``prepare`` places a message for the wire, independent of where it was created.
  ``inline`` (the default) keeps conversation order; ``pinned`` floats it to a named anchor: ``head``
  (after the system block — a graph-wide prefix), ``branch`` (this node's segment boundary), or ``tail``
  (the volatile end). Those three anchors are the whole vocabulary; pinning to an arbitrary message id is
  deliberately omitted. The cache-breakpoint policy keys on the anchor, not on pinned-ness — ``tail``
  takes a breakpoint *before* it, while ``head``/``branch`` are prefix anchors meant to stay cached, so
  their breakpoints fall after/at them.

Tags for both axes live in message metadata.

Both hooks fire on every model call, which is what makes payload delivery "as eager as possible":
anything posted to the ``PayloadQueue`` mid-run is ingested at the next model call of the current run,
not the next run.

Engines vary by agent kind (the root agent knows about modes and resources; a one-shot subagent needs
none of that). An agent descriptor builds its engine, wraps it in a ``PromptCompilerMiddleware``, and
returns it to the ``AgentRuntime`` registry so non-middleware callers (e.g. ``AgentSession``'s
post-mortem repair) reach the same instance. Reuse across engines is library-style: plain functions here
(and in ``engine.resources``) that concrete engines call from their own ``compile`` — the base class
provides a minimal *working* default, not a pipeline skeleton to hook into.
"""

from typing import Any, Awaitable, Callable, TYPE_CHECKING
from uuid import uuid4

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from rhizome.logs import get_logger

from ..base import AgentPayload, MessagePayload, StateUpdatePayload, Strategy
from .cleanup import apply_cleanup, mark_reclaimable
from .metadata import lifetime_of, set_role

if TYPE_CHECKING:
    from ..context import BaseAgentContext

_logger = get_logger("agent.engine.base")


# ========================================================================================================================
# COMPILE PRIMITIVES
# ========================================================================================================================
# Reusable pieces engines assemble in their own ``compile`` implementations. Plain functions on purpose —
# reuse here is a library, not a framework; no engine is obligated to call any of them.


def ensure_message_id(message: BaseMessage) -> BaseMessage:
    """Stamp a uuid4 id onto a message that lacks one.

    The ``add_messages`` reducer auto-assigns ids at reduce time anyway, but minting them at
    construction keeps the invariant ours: branch/merge identity (same id = same logical message
    across threads) and feed correlation should not hinge on a reducer implementation detail — and
    the compiler gets to know the id it just created before the update lands.
    """
    if message.id is None:
        message.id = str(uuid4())
    return message


def patch_orphaned_tool_calls(messages: list[BaseMessage], *, reason: str) -> list[ToolMessage]:
    """Synthesize ``ToolMessage`` results for tool calls that never received one.

    A run that dies between the model emitting ``tool_use`` blocks and the tool node completing leaves a
    checkpoint the Anthropic API will reject. Only the most recent ``AIMessage`` can be dangling, so only
    it is inspected. Idempotent: calls that already have results produce no patches.
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage) and m.tool_call_id}

    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            return [
                ensure_message_id(ToolMessage(content=reason, tool_call_id=tc["id"]))
                for tc in msg.tool_calls if tc["id"] not in answered
            ]
    return []


def payload_message(payload: MessagePayload) -> BaseMessage:
    """Convert a ``MessagePayload`` into a concrete message.

    System notifications become ``[System]``-prefixed human messages — several providers reject a
    ``SystemMessage`` anywhere but the head of the conversation.
    """
    match payload.role:
        case MessagePayload.Role.USER:
            return set_role(ensure_message_id(HumanMessage(content=payload.data)), "user")
        case MessagePayload.Role.AGENT:
            return set_role(ensure_message_id(AIMessage(content=payload.data)), "agent")
        case MessagePayload.Role.SYSTEM:
            msg = HumanMessage(content=f"<system>{payload.data}</system>")
            return set_role(ensure_message_id(msg), "system")
    raise ValueError(f"Unknown payload role: {payload.role}")


def ingest_payloads(payloads: list[AgentPayload], update: dict[str, Any]) -> None:
    """Fold drained payloads into a state-update dict (mutated in place).

    Messages accumulate under ``update["messages"]``; ``StateUpdatePayload`` fields merge at the top
    level, last-writer-wins *within* this batch — combining with existing state is the job of the state
    schema's reducers.
    """
    for payload in payloads:
        if isinstance(payload, MessagePayload):
            update.setdefault("messages", []).append(payload_message(payload))
        elif isinstance(payload, StateUpdatePayload):
            for key, value in payload.data.items():
                if key == "messages":
                    update.setdefault("messages", []).extend(value)
                else:
                    update[key] = value
        else:
            _logger.warning("Dropping unrecognized payload type: %s", type(payload).__name__)


# ========================================================================================================================
# ENGINE
# ========================================================================================================================


class PromptEngine[C]:
    """Base prompt engine: the compile/prepare/repair trifecta.

    Generic over ``C``, the context schema this engine is paired with on its ``AgentDeclaration`` — both
    ``compile`` and ``prepare`` receive the run's full state and that context (the live payload queue,
    resource stores, hooks, etc. all hang off it). The contract is exactly these three methods;
    everything else about an engine is private composition. The base implementation is the minimal
    working engine (history repair plus payload ingestion, no request shaping) and is suitable as-is for
    simple subagents. Richer engines override ``compile``/``prepare`` wholesale and assemble what they
    need from the compile primitives above.

    Message lifetime — reclaiming context (base-level; identification, cleanup, and branch-freeze live):

    Reclaiming context is generic to every conversation, so the machinery lives here, not on a particular
    engine kind — parameterized by policy a concrete engine supplies. Two stages, joined by the message's
    lifetime metadata tag:

    - *Identification* tags a message ``semi-permanent`` near its birth: tools self-tag where they can,
      and an auto-tagger retrofits the rest (a whitelist of bulky read-only tools plus a size threshold),
      re-emitting in place to set the tag. It also bakes a static ``reclaimable`` marker into the content
      (one-time, cache-stable) so the agent sees inline what it can free; a cleanup-group label joins
      that marker once cleanup lands.
    - *Cleanup* reclaims later, as in-place content replacement — never relocation or deletion, since a
      tool result cannot move from its tool call (the adjacency contract). It swaps the content for a
      stub or summary, keeps the id and slot, then promotes the message to ``permanent`` (a settled
      stub). Strategy is ``stub | stub+store | summarize | summarize+store`` (only ``stub`` built today),
      resolved message > request > engine default; ``apply_cleanup`` is the single owner of the mechanism.

    Request / execute split — the engine is the *only* emitter of cleanup edits. Everyone else — the
    workflow tools, app hooks, and the agent's own ``cleanup_context`` tool — files a declarative
    ``CleanupRequest{group, strategy?, reason?}`` onto the ``pending_cleanups`` channel on
    ``BaseAgentState`` (state, not context, so a request raised mid-stream survives a crash). The engine's
    one cleanup pass drains it and owns the policy: eligibility (resolved at fulfillment, so a since-pinned
    message is simply skipped) and the strategy default. A coarse auto-compact gate — letting it ignore
    requests wholesale — will ride on the (not-yet-built) auto-expiry trigger.

    Triggers and ownership — expiry is counted in user messages (a role metadata tag separates a real
    user turn from injected ``<system>`` human messages); budget-pressure, oldest-first reclamation is
    the intended north star. Default is opt-in replacement: a message reclaims on expiry unless the agent
    pinned it to ``permanent`` first. At a branch, inherited semi-permanent messages freeze to
    ``permanent`` so children share the parent's cached prefix (branch-point reclamation is deferred).
    Workflow tools scope one run by minting a run id into their proposal state and tagging that run's
    chatter ``group=<workflow>:<run-id>``, swept on accept.
    """

    DEFAULT_REPAIR_REASON = "Tool call was interrupted before a result could be recorded."

    DEFAULT_CLEANUP_STRATEGY: Strategy = "stub"
    """Engine default for reclaiming a message, when neither the message nor the request names one."""

    def __init__(
        self,
        reclaim_tools: frozenset[str] = frozenset(),
        reclaim_threshold: int = 0,
        expire_after: int | None = None,
    ) -> None:
        """Reclamation policy, inert by default and wired to options at build time eventually.
        ``reclaim_tools`` / ``reclaim_threshold`` configure the auto-tagger (identification): the tool
        names whose results auto-tag ``semi-permanent`` once their content passes ``reclaim_threshold``
        (content length — a stand-in for a token count). ``expire_after`` is the auto-compact gate: the
        number of genuine user turns after which a semi-permanent message auto-reclaims, or ``None`` to
        leave auto-expiry off (explicit ``cleanup_context`` requests are honored regardless)."""
        self._reclaim_tools = reclaim_tools
        self._reclaim_threshold = reclaim_threshold
        self._expire_after = expire_after

    async def compile(self, state: dict[str, Any], ctx: "C | None") -> dict[str, Any] | None:
        """Build the persistent state update applied ahead of the next model call."""
        update: dict[str, Any] = {}

        # Repair seeds the update's message list BEFORE payload ingestion appends to it. The order is
        # load-bearing: Anthropic requires a tool_result adjacent to its tool_use, and because runs
        # start with empty input (everything enters state through this update), nothing else can land
        # between the dangling tool call and its patch.
        patches = self.repair(state.get("messages", []))
        if patches:
            update["messages"] = list(patches)

        self._identify(state, update)        # stage 1 — auto-tag bulky tool results semi-permanent
        self._cleanup(state, ctx, update)    # stage 2 — reclaim (stub)

        if ctx is not None and ctx.pending is not None:
            ingest_payloads(ctx.pending.drain(), update)

        return update or None

    async def prepare(self, request: ModelRequest, ctx: "C | None") -> ModelRequest:
        """Reshape the outgoing model request. Per-request only — never persisted: the model node
        writes back only the model's OUTPUT, so request-message edits never reach the checkpoint
        (verified empirically in tests/agent/test_message_identity.py).

        Reshape via ``request.override(messages=...)`` — direct assignment to ``request.messages``
        is deprecated upstream.
        """
        return request

    def repair(self, messages: list[BaseMessage], *, reason: str | None = None) -> list[ToolMessage]:
        """Patch messages for orphaned tool calls.

        Pure and idempotent, so it is safe to run both in-stream (as part of ``compile``) and
        post-mortem (``AgentSession`` writing patches into the checkpoint after a broken run).
        Override to customize patch wording per agent kind.
        """
        return patch_orphaned_tool_calls(messages, reason=reason or self.DEFAULT_REPAIR_REASON)

    # ----- message lifetime ------------------------------------------------ #

    def _identify(self, state: dict[str, Any], update: dict[str, Any]) -> None:
        """Identification stage: auto-tag matching tool results ``semi-permanent``, re-emitting each with
        the inline marker (replace-in-place by id, so a message is marked exactly once). Tools that
        self-tagged at construction are already ``semi-permanent`` and skipped."""
        marked = [
            mark_reclaimable(m, group=m.name)
            for m in state.get("messages", [])
            if isinstance(m, ToolMessage) and lifetime_of(m) == "permanent" and self._should_autotag(m)
        ]
        if marked:
            update.setdefault("messages", []).extend(marked)

    def _should_autotag(self, message: ToolMessage) -> bool:
        """The auto-tagger's policy: a whitelisted tool whose textual result passes the size threshold."""
        return (
            message.name in self._reclaim_tools
            and isinstance(message.content, str)
            and len(message.content) >= self._reclaim_threshold
        )

    def _cleanup(self, state: dict[str, Any], ctx: "C | None", update: dict[str, Any]) -> None:
        """Cleanup stage: reclaim messages — explicit ``CleanupRequest``s drained from ``pending_cleanups``
        plus, when ``expire_after`` is set, those past their user-turn expiry — the engine the sole emitter
        of the edits (``apply_cleanup`` resolves eligibility + strategy). Drains the request queue (writes
        ``None``) once consumed. Branch-point promotion of inherited semi-permanent messages is the root
        engine's ``_freeze_inherited`` concern."""
        requests = state.get("pending_cleanups") or []
        if not requests and self._expire_after is None:
            return
        edits = apply_cleanup(
            state.get("messages", []), requests,
            expire_after=self._expire_after, default_strategy=self.DEFAULT_CLEANUP_STRATEGY,
        )
        if edits:
            update.setdefault("messages", []).extend(edits)
        if requests:
            update["pending_cleanups"] = None   # drain (the reducer resets to [])


# ========================================================================================================================
# MIDDLEWARE
# ========================================================================================================================


class PromptCompilerMiddleware(AgentMiddleware):
    """Thin shim binding a ``PromptEngine`` into an agent's middleware chain.

    Async-only by design (compilation needs async DB access) — agents carrying this middleware must be
    driven through ``astream``/``ainvoke``.

    Register it LAST in the middleware list so ``prepare`` wraps closest to the model and nothing
    reorders messages after cache breakpoints are placed (wrap hooks nest with earlier middleware
    outermost).

    The engine instance is shared by every conversation running on this agent, so it must hold no
    per-conversation state: per-thread facts belong in the agent state (a ``BaseAgentState`` subclass),
    per-conversation handles on the agent context (a ``BaseAgentContext`` subclass) the runtime binds
    per session.
    """

    def __init__(self, engine: PromptEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> PromptEngine:
        return self._engine

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        return await self._engine.compile(state, self._compile_context(runtime))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        request = await self._engine.prepare(request, self._compile_context(request.runtime))
        return await handler(request)

    @staticmethod
    def _compile_context(runtime) -> "BaseAgentContext | None":
        # langgraph hangs the per-run context off ``runtime.context``; the engine reads its live channels
        # (payload queue, resource stores, hooks) straight from there.
        return getattr(runtime, "context", None)
