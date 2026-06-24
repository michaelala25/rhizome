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
    SystemMessage,
    ToolMessage,
)

from rhizome.logs import get_logger

from ..base import AgentPayload, MessagePayload, StateUpdatePayload, Strategy
from .cleanup import apply_cleanup, apply_hydrations, mark_reclaim_ineligible, mark_reclaimable, Summarizer
from .metadata import is_reclaim_ineligible, lifetime_of, role_of, set_role
from .usage import (
    estimate_message_tokens,
    estimate_system_tokens,
    estimate_tool_tokens,
    normalize,
    countable,
    provider_usage,
    tool_kind,
    ProviderUsage,
    UsageReport,
    UsageSegment,
)

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

    Request / execute split — the engine is the *only* emitter of lifetime edits. Everyone else — the
    workflow tools, app hooks, and the agent's own ``cleanup_context`` / ``hydrate`` tools — files a
    declarative ``CleanupRequest{group, strategy?, reason?}`` / ``HydrateRequest{group}`` onto the
    ``pending_cleanups`` / ``pending_hydrations`` channels on ``BaseAgentState`` (state, not context, so a
    request raised mid-stream survives a crash). The engine's one cleanup pass drains them and owns the
    policy: eligibility (resolved at fulfillment, so a since-pinned message is simply skipped), the strategy
    default, and the master gate — ``_reclaim_on()`` off honors nothing and just drains.

    Triggers and ownership — expiry is counted in user messages (a role metadata tag separates a real
    user turn from injected ``<system>`` human messages); budget-pressure, oldest-first reclamation is
    the intended north star. Default is opt-in replacement: a message reclaims on expiry unless the agent
    keeps it (``hydrate`` pushes its expiry out, and after ``MAX_HYDRATIONS`` keeps promotes it to
    ``permanent``). At a branch, inherited semi-permanent messages freeze to ``permanent`` so children share
    the parent's cached prefix (branch-point reclamation is deferred). The reclamation policy (on/off,
    threshold, expiry age) is read through ``_reclaim_on`` / ``_effective_threshold`` / ``_effective_expiry``
    — constants here, live options on the root engine. Workflow tools scope one run by minting a run id into
    their proposal state and tagging that run's chatter ``group=<workflow>:<run-id>``, swept on accept.
    """

    DEFAULT_REPAIR_REASON = "Tool call was interrupted before a result could be recorded."

    DEFAULT_CLEANUP_STRATEGY: Strategy = "stub"
    """Engine default for reclaiming a message, when neither the message nor the request names one."""

    HYDRATE_BUMP_TURNS = 5
    """User turns ``hydrate`` adds to a kept message's expiry each time the agent keeps it."""

    MAX_HYDRATIONS = 3
    """After this many hydrations a kept message is promoted to ``permanent`` instead of bumped again — it
    has been kept enough times to simply settle (the freeze fallback, so ``hydrate`` needs no twin tool)."""

    def __init__(
        self,
        reclaim_tools: frozenset[str] = frozenset(),
        reclaim_threshold: int = 0,
        expire_after: int | None = None,
        *,
        system_prompt: str | None = None,
        tools: list | None = None,
        max_input_tokens: int | None = None,
    ) -> None:
        """Reclamation policy, inert by default and wired to options at build time eventually.
        ``reclaim_tools`` / ``reclaim_threshold`` configure the auto-tagger (identification): the tool
        names whose results auto-tag ``semi-permanent`` once their content passes ``reclaim_threshold``
        (content length — a stand-in for a token count). ``expire_after`` is the auto-compact gate: the
        number of genuine user turns after which a semi-permanent message auto-reclaims, or ``None`` to
        leave auto-expiry off (explicit ``cleanup_context`` requests are honored regardless).

        ``system_prompt`` / ``tools`` / ``max_input_tokens`` are the build-time constants ``report`` needs
        for token accounting: the system block and tool-definition sizes (fixed for the agent's lifetime —
        the engine rebuilds when they change, so counting once here is correct) and the model's context
        window. All optional: an engine built without them still reports per-message usage, just with no
        system/tools slice and no window percentage."""
        self._reclaim_tools = reclaim_tools
        self._reclaim_threshold = reclaim_threshold
        self._expire_after = expire_after

        self._system_tokens = estimate_system_tokens(system_prompt)
        self._tool_tokens = estimate_tool_tokens(tools)
        self._max_input_tokens = max_input_tokens

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

        self._identify(state, update)              # stage 1 — auto-tag bulky tool results semi-permanent
        await self._cleanup(state, ctx, update)    # stage 2 — reclaim (stub/summarize)

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

    # ----- usage accounting ------------------------------------------------ #

    def report(self, state_values: dict[str, Any]) -> UsageReport:
        """Account this thread's token usage from its checkpointed state — the provider ground truth plus a
        normalized per-message breakdown of the current prompt (see ``engine.usage``). Pure and synchronous:
        the caller fetches state (the engine doesn't own the thread) and hands the values in, e.g.
        ``engine.report(await session.agent_state)``.

        The breakdown is normalized to the provider's reported ``input_tokens``, so it folds in the
        system-prompt and tool-definition cost and agrees with the headline. Before the thread's first model
        response there is no ground truth to normalize against, so the segments carry raw estimates."""
        messages = state_values.get("messages", [])
        usage: ProviderUsage | None = provider_usage(messages)
        segments = self._raw_segments(messages)
        target = usage.input_tokens if usage is not None else None
        return UsageReport(usage, self._max_input_tokens, tuple(normalize(segments, target)))

    def _raw_segments(self, messages: list[BaseMessage]) -> list[UsageSegment]:
        """The current prompt's slices with raw (un-normalized) estimates: the synthetic system and
        tool-definition blocks first (the fixed prefix), then one slice per real message."""
        segments: list[UsageSegment] = []
        if self._system_tokens:
            segments.append(UsageSegment("system", self._system_tokens))
        if self._tool_tokens:
            segments.append(UsageSegment("tools", self._tool_tokens))
        segments.extend(self._message_segment(m) for m in countable(messages))
        return segments

    def _message_segment(self, message: BaseMessage) -> UsageSegment:
        """One message's usage slice: its estimated size, tagged by id and a kind. ``_message_kind`` is the
        classification seam a richer engine overrides to recognize its own message ids."""
        return UsageSegment(self._message_kind(message), estimate_message_tokens(message), message_id=message.id)

    def _message_kind(self, message: BaseMessage) -> str:
        """Generic message classification by type and role tag. A tool round-trip splits into ``tool_use``
        (the model's invocation, carried on an ``AIMessage``) and ``tool_result`` (a ``ToolMessage``) — see
        ``tool_kind``; a plain ``AIMessage`` is ``agent``; and a ``HumanMessage`` is a genuine ``user`` turn
        unless it is an injected ``<system>`` message (role-tagged at construction — branch markers, mode
        notices), which is a ``system_notice``."""
        tk = tool_kind(message)
        if tk is not None:
            return f"tool_{tk}"        # tool_use | tool_result
        if isinstance(message, AIMessage):
            return "agent"
        if isinstance(message, SystemMessage):
            return "system"
        if isinstance(message, HumanMessage):
            return "system_notice" if role_of(message) == "system" else "user"
        return "other"

    # ----- message lifetime ------------------------------------------------ #
    # Reclamation policy read through three accessors so a richer engine can source it from live options
    # without the base knowing about them: the base returns its build-time constants, ``RootPromptEngine``
    # overrides these to read its ``AutoCompact`` / ``AutoCompactAfter`` / ``AutoCompactThreshold`` handles.

    def _reclaim_on(self) -> bool:
        """Whether reclamation is active at all. The base engine has no master toggle, so it is always on
        (identification is still gated by the ``reclaim_tools`` whitelist); the root engine overrides this
        to read its live ``AutoCompact`` toggle."""
        return True

    def _effective_threshold(self) -> int:
        """The auto-tagger's size threshold, in approximate tokens (the root engine overrides to read its
        live ``AutoCompactThreshold``)."""
        return self._reclaim_threshold

    def _effective_expiry(self) -> int | None:
        """The default auto-expiry age in user turns, or ``None`` for off (the root engine overrides to read
        its live ``AutoCompactAfter``)."""
        return self._expire_after

    def _identify(self, state: dict[str, Any], update: dict[str, Any]) -> None:
        """Identification stage: evaluate each un-evaluated permanent tool result exactly once. A match
        auto-tags ``semi-permanent`` and bakes the inline marker; a miss stamps ``reclaim_ineligible`` so
        no later pass re-sizes it (the off-wire stamp is cache-free; see ``mark_reclaim_ineligible``). Both
        re-emit in place by id. Tools that self-tagged are already ``semi-permanent`` and skipped, as are
        messages a prior pass found ineligible.

        Inert unless an auto-tagger is configured AND reclamation is on: an empty ``reclaim_tools`` opts out
        of auto-tagging entirely, and ``_reclaim_on()`` off (the master toggle) pauses it — either way it
        stamps nothing (self-tagging tools and explicit requests still work)."""
        if not self._reclaim_tools or not self._reclaim_on():
            return
        emitted: list[BaseMessage] = []
        for m in state.get("messages", []):
            if not isinstance(m, ToolMessage) or lifetime_of(m) != "permanent" or is_reclaim_ineligible(m):
                continue
            emitted.append(mark_reclaimable(m, group=m.name) if self._should_autotag(m)
                           else mark_reclaim_ineligible(m))
        if emitted:
            update.setdefault("messages", []).extend(emitted)

    def _should_autotag(self, message: ToolMessage) -> bool:
        """The auto-tagger's policy: a whitelisted tool whose textual result passes the size threshold (an
        approximate token count, the same estimator ``report`` uses)."""
        return (
            message.name in self._reclaim_tools
            and isinstance(message.content, str)
            and estimate_message_tokens(message) >= self._effective_threshold()
        )

    async def _cleanup(self, state: dict[str, Any], ctx: "C | None", update: dict[str, Any]) -> None:
        """Cleanup stage: apply the filed lifetime requests and age expiry — the engine the sole emitter of
        the edits. Reclaim (``CleanupRequest``s from ``pending_cleanups`` plus messages past their user-turn
        expiry — own ``expire_after`` else ``_effective_expiry()``), then hydrate (``HydrateRequest``s from
        ``pending_hydrations`` — push expiry out, or promote once hydrated enough). Hydrate edits land LAST,
        so a deliberate keep wins over the same message's auto-expiry this turn. Drains both queues once
        consumed. When reclamation is toggled off, honors nothing but still drains, so a stray request never
        fires on re-enable. Branch-point promotion of inherited messages is ``_freeze_inherited``'s concern."""
        cleanups = state.get("pending_cleanups") or []
        hydrations = state.get("pending_hydrations") or []
        messages = state.get("messages", [])

        if not self._reclaim_on():
            if cleanups:
                update["pending_cleanups"] = None
            if hydrations:
                update["pending_hydrations"] = None
            return

        # A per-message ``expire_after`` can fire even with the engine default off, so the cheap skip keys on
        # the presence of a target (a request of either kind, or any semi-permanent message), not the default.
        if not cleanups and not hydrations and not any(lifetime_of(m) == "semi-permanent" for m in messages):
            return

        edits = await apply_cleanup(
            messages, cleanups,
            expire_after=self._effective_expiry(), default_strategy=self.DEFAULT_CLEANUP_STRATEGY,
            summarize=self._summarizer(ctx),
        )
        if hydrations:
            edits = edits + apply_hydrations(
                messages, hydrations, default_expiry=self._effective_expiry(),
                bump=self.HYDRATE_BUMP_TURNS, max_hydrations=self.MAX_HYDRATIONS,
            )
        if edits:
            update.setdefault("messages", []).extend(edits)
        if cleanups:
            update["pending_cleanups"] = None   # drain (the reducer resets to [])
        if hydrations:
            update["pending_hydrations"] = None

    def _summarizer(self, ctx: "C | None") -> Summarizer | None:
        """Hook: the batch summarizer for ``summarize``-strategy reclamations, or ``None`` to fall back to
        stub. Base engines have none (stub-only); the root engine overrides it to run a summarizer subagent
        off ``ctx.runtime`` and bracket the work with compaction events."""
        return None


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
