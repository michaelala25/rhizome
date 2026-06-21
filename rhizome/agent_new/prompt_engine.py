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
post-mortem repair) reach the same instance. Reuse across engines is library-style: plain functions in
this module that concrete engines call from their own ``compile`` — the base class provides a minimal
*working* default, not a pipeline skeleton to hook into.
"""

from typing import Any, Awaitable, Callable, Iterable, TYPE_CHECKING
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
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from rhizome.logs import get_logger
from rhizome.resources_new import (
    build_index_block,
    build_resource_block,
    load_delta,
    ResourceContextStore,
    ResourceLoadDelta,
    ResourceTree,
    ResourceTreeNode,
)

from .cleanup import apply_cleanup, mark_reclaimable, promote
from .metadata import lifetime_of, pin, Pin, pin_of, set_role, Strategy
from .payload import AgentPayload, MessagePayload, StateUpdatePayload
from .prompts import (
    LEARN_MODE_GUIDE,
    LEARN_MODE_REMINDER,
    REVIEW_MODE_GUIDE,
    REVIEW_MODE_REMINDER,
    render_tool_allowlist,
)
from .state import ConsumedResources

if TYPE_CHECKING:
    from .context import BaseAgentContext, RootAgentContext

_logger = get_logger("agent.prompt_engine")


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


def resource_deltas(
    consumed: ConsumedResources | None,
    ctx: "RootAgentContext",
) -> tuple[ResourceLoadDelta, ResourceLoadDelta, ConsumedResources]:
    """Per-channel deltas between this thread's consumed snapshot and the stores' desired state, plus
    the fresh snapshot to persist back to ``AgentState.consumed_resource_context``.

    Returns ``(global_delta, local_delta, snapshot)``. An engine's resource pass applies the deltas
    as message edits — fetch content for additions, drop blocks for removals, against the global and
    per-node local well-known message IDs respectively — and writes the snapshot into the SAME state
    update, so consumption becomes a fact exactly when the content lands.

    The effective local desire is the local store minus whatever the global store already covers —
    the concomitance backstop. It is entry-level skip-don't-fix: the stores are never mutated (the
    writer-side policy owns the invariant), double-injection is suppressed, and the suppression
    self-heals in both directions (a node skipped while globally covered re-enters the local delta
    the moment global coverage retreats, because it was never recorded as consumed). Partial subtree
    overlaps below entry granularity remain the writer's responsibility.
    """
    consumed_global = (consumed or {}).get("global") or []
    consumed_local = (consumed or {}).get("local") or []

    global_loaded = list(ctx.global_resources.loaded) if ctx.global_resources is not None else []
    local_loaded = list(ctx.local_resources.loaded) if ctx.local_resources is not None else []
    if ctx.global_resources is not None:
        local_loaded = [n for n in local_loaded if not ctx.global_resources.is_loaded(n)]

    return (
        load_delta(consumed_global, global_loaded),
        load_delta(consumed_local, local_loaded),
        ConsumedResources(**{"global": global_loaded, "local": local_loaded}),
    )


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
        (verified empirically in tests/agent_new/test_message_identity.py).

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
    per-conversation state: per-thread facts belong in ``AgentState``, per-conversation handles on the
    agent context (a ``BaseAgentContext`` subclass) the runtime binds per session.
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


# ========================================================================================================================
# ROOT ENGINE
# ========================================================================================================================


GLOBAL_RESOURCE_MESSAGE_PREFIX = "global-resource-ctx-"


def global_resource_message_id(resource_id: int) -> str:
    """Deterministic, thread-stable id for a resource's global context message — one per resource. The
    ``add_messages`` reducer replaces it in place on a content change and ``RemoveMessage`` drops it on
    unload; ``prepare`` locates the block by this prefix."""
    return f"{GLOBAL_RESOURCE_MESSAGE_PREFIX}{resource_id}"


def is_global_resource_message(message: BaseMessage) -> bool:
    return bool(message.id) and message.id.startswith(GLOBAL_RESOURCE_MESSAGE_PREFIX)


LOCAL_RESOURCE_MESSAGE_PREFIX = "local-resource-ctx-"


def local_resource_message_id(resource_id: int) -> str:
    """Per-resource id for a node-local context message. Keyed by resource (not node) so it inherits
    cleanly across a branch — the child manages the same id it was seeded, replace-in-place on a content
    change, ``RemoveMessage`` on unload — exactly like the global block but a distinct prefix and segment.
    The concomitance backstop in ``resource_deltas`` keeps a globally-covered resource out of this set, so
    the two channels never inject the same resource at once."""
    return f"{LOCAL_RESOURCE_MESSAGE_PREFIX}{resource_id}"


def is_local_resource_message(message: BaseMessage) -> bool:
    return bool(message.id) and message.id.startswith(LOCAL_RESOURCE_MESSAGE_PREFIX)


INDEX_RESOURCE_MESSAGE_ID = "index-resource-ctx"


def is_index_resource_message(message: BaseMessage) -> bool:
    """The vector index's "what's queryable" reminder — a SINGLE message (not one per resource, unlike
    the context channels), rebuilt in place on a load change by ``compile`` and floated to the ephemeral
    tail by ``prepare``."""
    return message.id == INDEX_RESOURCE_MESSAGE_ID


BRANCH_MARKER_PREFIX = "branch-marker-"


def branch_marker_message_id(node_id: int) -> str:
    """Deterministic, per-node id for a conversation node's branch marker — one per node, so the engine
    injects it exactly once (idempotent by id) and it sits at the node's segment boundary."""
    return f"{BRANCH_MARKER_PREFIX}{node_id}"


def _branch_marker_content(parent_name: str | None) -> str:
    source = f'"{parent_name}"' if parent_name else "an earlier point in the conversation"
    return (
        f"<system>This conversation branched from {source}. The messages above are inherited context "
        f"up to the branch point.</system>"
    )


MODE_GUIDE_PREFIX = "mode-guide-"


def mode_guide_message_id(mode: str) -> str:
    """Deterministic id for a mode's full-guide message — one per mode. Its presence in the thread is the
    'have we entered this mode before' signal: absent -> inject the full guide under this id; present ->
    the guide is already in context, so a re-entry posts a concise reminder instead."""
    return f"{MODE_GUIDE_PREFIX}{mode}"


# Full guide + concise reminder per mode. ``idle`` is absent on purpose — its behaviour is covered by the
# shared system prompt, so an idle switch posts only a one-line notice plus the tool allowlist.
_MODE_GUIDES: dict[str, tuple[str, str]] = {
    "learn": (LEARN_MODE_GUIDE, LEARN_MODE_REMINDER),
    "review": (REVIEW_MODE_GUIDE, REVIEW_MODE_REMINDER),
}


def _owning_resource_id(node: ResourceTreeNode, tree: ResourceTree) -> int | None:
    """Walk up to the ``("resource", rid)`` root that owns ``node`` — pure tree traversal, no DB. A
    resource node answers immediately (so a deleted resource still maps to its id); a section whose owner
    has left the tree yields ``None``."""
    current: ResourceTreeNode | None = node
    while current is not None:
        if current.kind == "resource":
            return current.id
        current = tree.parent(current)
    return None


def _group_by_resource(
    nodes: Iterable[ResourceTreeNode], tree: ResourceTree
) -> dict[int, list[ResourceTreeNode]]:
    """Group a flat set of nodes by the ``("resource", rid)`` root that owns each — the by-resource
    shape the index listing wants. Nodes whose owner has left the tree are dropped."""
    grouped: dict[int, list[ResourceTreeNode]] = {}
    for node in nodes:
        rid = _owning_resource_id(node, tree)
        if rid is not None:
            grouped.setdefault(rid, []).append(node)
    return grouped


def _block_builder(session_factory, resource_id: int, nodes: list[ResourceTreeNode]):
    """A no-arg async thunk that opens its own session to build one resource's block. Handed to
    ``store.block`` so the DB read fires only on a cache miss — a warm global store serves branches
    without touching the DB."""
    async def build() -> str | None:
        async with session_factory() as session:
            return await build_resource_block(session, resource_id, nodes)
    return build


class RootPromptEngine(PromptEngine["RootAgentContext"]):
    """Prompt engine for the root conversation agent.

    ``compile`` persists the durable facts — resource context messages and their consumed snapshot, the
    vector-index reminder and its snapshot, branch markers — and ``prepare`` arranges the floating blocks
    for each request. Implemented today: global and local resource context, the index reminder, branch
    markers, and modes. The rest of the target layout — the ``pinned``/``inline`` position scheme, cache
    breakpoints, the index rendered as a derived message, and semi-permanent reclamation (whose machinery
    lives on ``PromptEngine``) — is the roadmap below.

    Target prompt layout (positions assigned by tag, arranged per-request by ``prepare``):

        [system prompt]          <- fixed; never swapped per agent mode
        [global resources]       <- pin: head
        <breakpoint>             <- after head, only past a size threshold
        [prompt segment]         <- branch points along the lineage (graph topology)
        <breakpoint>             <- at the last branch point before the leaf
        [local resources]        <- pin: branch (this node's segment boundary)
        [leaf segment]
        <breakpoint>             <- before the first semi-permanent message
        [semi-permanent span]    <- first semi-perm message .. just before the first tail pin
        <breakpoint>             <- before the tail (always)
        [tail / pin: tail]       <- derived ephemerals: the index reminder, the reclaimable-context
                                    status message, volatile reminders

    The semi-permanent span is a *positional* region, not a relocation — messages stay in conversation
    order; the span simply runs from the first semi-permanent message to the first tail pin. Because
    cleanup promotes a reclaimed message to ``permanent``, the span's start advances as reclamation
    proceeds, so the breakpoint before it and the cache boundary are one and the same moving line.

    Breakpoint placement keys on the pin anchor, not on pinned-ness:
        - before the tail — always (the volatile end; cheapest suffix to reprice)
        - at branch points — highest value (keeps prefixes warm when swapping between branches)
        - after global (head) resources — only past a size threshold
        - before the semi-permanent span — stable prefix across reclamation (typically only the leaf
          reprices)

    The index reminder is a *derived* message: ``compile`` still calls ``resource_index.consume()`` (a
    real advance of the live store), but the "what's queryable" block is rendered in ``prepare`` from the
    current loaded set (memoized) and pinned to the tail — no ``consumed_resource_index`` snapshot, no
    stable-id replace, no special-case float. The reclaimable-context status message (the agent's window
    onto what it can free: each group, its size, and turns-until-auto-clean) is derived the same way.

    ``compile`` injects conversation-order notifications on state changes:
        - first entry into a mode -> full mode guide; re-entry -> brief reminder; idle -> bare notice
        - the tool allowlist for the new mode, in the same block

    Open questions / ideas:
        - budget-pressure reclamation (oldest-first, only when over a token budget) as the trigger,
          beyond the fixed user-message-count expiry of the first cut
        - the summary strategy: a stateless summarizer subagent plus a stash the agent re-hydrates by key
        - auto-hydrate: periodic no-op requests just to keep prefixes warm (build on top, later)
        - prefix-cache canary: checksum prefixes after ``prepare``; alert when a stable prefix shifts
    """

    async def compile(self, state: dict[str, Any], ctx: "RootAgentContext | None") -> dict[str, Any] | None:
        update: dict[str, Any] = {}

        # Repair patches lead — the adjacency contract (see PromptEngine.compile).
        patches = self.repair(state.get("messages", []))
        if patches:
            update["messages"] = list(patches)

        self._identify(state, update)        # stage 1 — base capability (see PromptEngine._identify)
        self._cleanup(state, ctx, update)    # stage 2 — reclaim (stub)

        if ctx is not None:
            # Branch marker first — it anchors this node's segment boundary (see _compile_branch_marker).
            self._compile_branch_marker(state, ctx, update)
            # Freeze inherited semi-permanent messages to permanent — after _cleanup, so its promotions win
            # over any concurrent expiry of the same inherited message (add_messages is last-writer by id).
            self._freeze_inherited(state, ctx, update)
            # Context pass: per-resource content messages (global + local) + the consumed snapshot.
            await self._compile_resources(state, ctx, update)
            # Index pass: lazy ingestion + the single "what's queryable" reminder + its snapshot.
            await self._compile_index(state, ctx, update)
            # Mode pass: witness an app_state-vs-state mode switch, commit it, narrate it (guide/header).
            self._compile_mode(state, ctx, update)
            # Payload ingestion last.
            if ctx.pending is not None:
                ingest_payloads(ctx.pending.drain(), update)

        return update or None

    async def prepare(self, request: ModelRequest, ctx: "RootAgentContext | None") -> ModelRequest:
        """Float each pinned message to its anchor for this one wire request (view-only — never
        persisted, message identity untouched, so it composes with the facts the persist side writes).
        Inline messages keep conversation order; a pinned message (tagged in ``additional_kwargs`` —
        see ``metadata.py``) moves to:

        - ``head`` — just after the leading system block: a stable graph-wide prefix;
        - ``branch`` — the start of THIS node's segment, right after its branch marker (or, for a node
          with no marker — the root — the body head);
        - ``tail`` — the very end (the volatile region a breakpoint will sit before); a change there
          invalidates only the cheapest suffix, never the prefix up to the leaf.

        Floating keeps the prefix up to the leaf stable for the cache no matter where a block was loaded.
        Returns the request untouched when nothing is pinned."""
        buckets: dict[Pin, list[BaseMessage]] = {"head": [], "branch": [], "tail": []}
        inline: list[BaseMessage] = []
        for m in request.messages:
            anchor = pin_of(m)
            (buckets[anchor] if anchor in buckets else inline).append(m)

        if not any(buckets.values()):
            return request

        # The leading system block stays at the head; head pins sit just after it.
        cut = 0
        while cut < len(inline) and isinstance(inline[cut], SystemMessage):
            cut += 1
        system, body = inline[:cut], inline[cut:]

        # The branch anchor: right after THIS node's marker, or the body head if it has none (the root).
        split = 0
        if buckets["branch"] and ctx is not None and ctx.node_id is not None:
            marker_id = branch_marker_message_id(ctx.node_id)
            split = next((i + 1 for i, m in enumerate(body) if m.id == marker_id), 0)

        return request.override(messages=[
            *system, *buckets["head"], *body[:split], *buckets["branch"], *body[split:], *buckets["tail"],
        ])

    # ----- branch marker --------------------------------------------------- #

    def _compile_branch_marker(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Inject this node's branch marker once, on the first compile after it was branched.

        Witnesses the topology (pull): a node that has a parent and hasn't yet recorded its marker gets a
        ``<system>`` message noting the fork. Self-positioning — appended on the first compile, it lands
        right after the inherited prefix (the segment boundary) and stays there, the anchor that local
        resources will later cluster against. The root (no parent) and graph-less sessions get nothing.
        """
        if ctx.topology is None or ctx.node_id is None:
            return
        snapshot = ctx.topology.snapshot
        info = snapshot.node(ctx.node_id)
        if info is None or not info.parents:
            return   # the root, or this node isn't in the published snapshot yet

        message_id = branch_marker_message_id(ctx.node_id)
        if any(m.id == message_id for m in state.get("messages", [])):
            return   # already injected — idempotent across this node's runs

        parent = snapshot.node(info.parents[0])
        content = _branch_marker_content(parent.name if parent is not None else None)
        update.setdefault("messages", []).append(HumanMessage(content=content, id=message_id))

    def _freeze_inherited(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Branch-point freeze, a per-compile contract driven by the topology: every semi-permanent
        message before this node's branch marker is inherited context, so promote it to ``permanent`` —
        content untouched, so the child's prefix stays byte-identical to the parent's (cache-shared) and
        is never re-reclaimed; only the node's OWN later messages stay reclaimable. Idempotent. On the
        first compile the marker isn't in state yet, so everything currently in state was inherited and is
        frozen wholesale."""
        if ctx.topology is None or ctx.node_id is None:
            return
        info = ctx.topology.snapshot.node(ctx.node_id)
        if info is None or not info.parents:
            return   # the root, or a node not yet in the published snapshot — nothing inherited
        messages = state.get("messages", [])
        marker_id = branch_marker_message_id(ctx.node_id)
        cut = next((i for i, m in enumerate(messages) if m.id == marker_id), len(messages))
        for m in messages[:cut]:
            if lifetime_of(m) == "semi-permanent":
                update.setdefault("messages", []).append(promote(m))

    # ----- resource pass --------------------------------------------------- #

    async def _compile_resources(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Diff the global and local stores against this thread's consumed snapshot; for every resource
        whose load state changed on either channel, emit a context message (rebuilt block or
        ``RemoveMessage``) keyed per resource per channel, and write the fresh snapshot into the SAME
        update — so consumption becomes a fact exactly when the content lands.

        ``resource_deltas`` computes both deltas and the snapshot in one pass; its concomitance backstop
        already subtracts global coverage from the local set, so the channels never inject the same
        resource at once. Each channel's messages are built from its snapshot list (the desired,
        concomitance-adjusted nodes), not the raw store."""
        if ctx.session_factory is None or (ctx.global_resources is None and ctx.local_resources is None):
            return

        global_delta, local_delta, snapshot = resource_deltas(state.get("consumed_resource_context"), ctx)
        if not global_delta and not local_delta:
            return

        messages: list[BaseMessage] = []
        if ctx.global_resources is not None:
            messages += await self._resource_messages(
                global_delta, snapshot["global"], ctx.global_resources, ctx.session_factory,
                global_resource_message_id, "head",
            )
        if ctx.local_resources is not None:
            messages += await self._resource_messages(
                local_delta, snapshot["local"], ctx.local_resources, ctx.session_factory,
                local_resource_message_id, "branch",
            )
        if messages:
            update.setdefault("messages", []).extend(messages)
        update["consumed_resource_context"] = snapshot

    @staticmethod
    async def _resource_messages(
        delta: ResourceLoadDelta,
        desired: list[ResourceTreeNode],
        store: ResourceContextStore,
        session_factory,
        message_id: Callable[[int], str],
        anchor: Pin,
    ) -> list[BaseMessage]:
        """One message per resource touched by ``delta`` on one channel: a block rebuilt from the
        resource's *current* desired nodes (``desired`` — that channel's snapshot list), or a
        ``RemoveMessage`` once it has none left. The block is rebuilt wholesale, since a resource's
        message is the union of its loaded sections. ``message_id`` namespaces the channel (global vs
        local) and ``anchor`` pins the block to its layout slot (``head`` for global, ``branch`` for
        local); ``store`` supplies the shared tree and the optional content cache."""
        if not delta:
            return []
        tree = store.tree

        touched: set[int] = set()
        for node in (*delta.additions, *delta.removals):
            rid = _owning_resource_id(node, tree)
            if rid is not None:
                touched.add(rid)

        # Regroup the channel's desired nodes by owning resource — only for the touched ones.
        current: dict[int, list[ResourceTreeNode]] = {rid: [] for rid in touched}
        for node in desired:
            rid = _owning_resource_id(node, tree)
            if rid in current:
                current[rid].append(node)

        messages: list[BaseMessage] = []
        for rid in sorted(touched):
            nodes = current[rid]
            mid = message_id(rid)
            if not nodes:
                messages.append(RemoveMessage(id=mid))
                continue
            content = await store.block(rid, _block_builder(session_factory, rid, nodes))
            if content is not None:
                messages.append(pin(HumanMessage(content=content, id=mid), anchor))
        return messages

    # ----- index pass ------------------------------------------------------ #

    async def _compile_index(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Lazily ingest the index, then refresh this thread's "what's queryable" reminder.

        ``consume()`` advances the graph-global vector index (incremental, idempotent — a steady-state
        call does nothing). The reminder itself is per-thread: diff the index store's desired ``loaded``
        set against this thread's snapshot in ``consumed_resource_index`` and, only on a change, rebuild
        the single stable-id message (or drop it) and write the snapshot into the SAME update. ``prepare``
        floats the message to the ephemeral tail, so a change here never disturbs the cached prefix.
        """
        if ctx.resource_index is None or ctx.session_factory is None:
            return
        await ctx.resource_index.consume()

        loaded = ctx.resource_index.loaded
        consumed = set(state.get("consumed_resource_index") or [])
        if consumed == loaded:
            return

        grouped = _group_by_resource(loaded, ctx.resource_index.tree)
        content = await self._index_block(grouped, ctx.session_factory) if grouped else None
        if content is not None:
            update.setdefault("messages", []).append(
                pin(HumanMessage(content=content, id=INDEX_RESOURCE_MESSAGE_ID), "tail")
            )
        elif consumed:
            # The reminder existed and now has nothing to announce -> drop it. Guarded on a non-empty
            # prior snapshot: a RemoveMessage for an id that was never injected would raise.
            update.setdefault("messages", []).append(RemoveMessage(id=INDEX_RESOURCE_MESSAGE_ID))
        update["consumed_resource_index"] = list(loaded)

    @staticmethod
    async def _index_block(grouped: dict[int, list[ResourceTreeNode]], session_factory) -> str | None:
        """Open a session to render the index listing — mirrors ``_block_builder`` but for the single
        index message, so the DB read fires only when the loaded set actually changed."""
        async with session_factory() as session:
            return await build_index_block(session, grouped)

    # ----- mode pass ------------------------------------------------------- #

    def _compile_mode(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Witness a mode switch on the ``AppContextStore`` (the SSOT) and react.

        ``app_state.mode`` carries desire (the user via the view, or the agent via ``set_mode``);
        ``AgentState["mode"]`` is the engine-committed fact. They match in steady state, so the diff is
        the whole trigger — equal means nothing happened. On a switch, commit the new mode and append one
        ``<system>`` message narrating it (full guide on first entry, concise reminder on re-entry, plus
        the tool allowlist). Default ``idle`` is the baseline, so a fresh thread sitting in idle says
        nothing; only a real deviation speaks. Raw conversation context — appended here, never floated.
        """
        if ctx.app_state is None:
            return
        desired = ctx.app_state.mode
        if desired == (state.get("mode") or "idle"):
            return

        update["mode"] = desired
        update.setdefault("messages", []).append(self._mode_switch_message(desired, state))

    @staticmethod
    def _mode_switch_message(mode: str, state: dict[str, Any]) -> BaseMessage:
        """Build the one message a switch into ``mode`` posts: full guide the first time the mode is
        entered (keyed by ``mode_guide_message_id`` so re-entries are detectable), a concise reminder on
        re-entry (fresh id — it belongs at the current point), or a bare notice for idle. The tool
        allowlist is appended in the same block."""
        guide = _MODE_GUIDES.get(mode)
        first_entry = guide is not None and not any(
            m.id == mode_guide_message_id(mode) for m in state.get("messages", [])
        )

        if guide is not None and first_entry:
            body, tag, message_id = guide[0], "system", mode_guide_message_id(mode)
        elif guide is not None:
            body, tag, message_id = guide[1], "system-reminder", None
        else:
            body, tag, message_id = f"You are now in **{mode}** mode.", "system", None

        content = f"<{tag}>\n{body}\n\n{render_tool_allowlist(mode)}\n</{tag}>"
        return ensure_message_id(HumanMessage(content=content, id=message_id))
