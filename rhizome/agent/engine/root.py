"""``RootPromptEngine``: the prompt engine for the root conversation agent.

Builds on ``engine.base.PromptEngine`` (the compile/prepare/repair contract and the message-lifetime
machinery) and ``engine.resources`` (the resource-context helpers). ``compile`` adds the root agent's
durable facts — global/local resource context blocks, the vector-index reminder, branch markers, mode
guides — and ``prepare`` arranges the floating blocks for each wire request.

The well-known message ids for resource/index blocks live in ``engine.resources``; the branch-marker and
mode-guide id schemes live here, beside the engine that owns them.
"""

import asyncio
from typing import Any, Callable, TYPE_CHECKING

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage

from rhizome.logs import get_logger
from rhizome.resources import ResourceContextStore, ResourceLoadDelta, ResourceTreeNode

from ..base import MessagePayload
from .base import ensure_message_id, ingest_payloads, PromptEngine
from .cache import allocate, Breakpoint, cache_control, CacheControl, is_annotatable, OptionReader
from .cleanup import promote, reclamation_status, Summarizer
from .dump import dump_report, dump_request
from .metadata import lifetime_of, pin, Pin, pin_of, set_role
from .resources import (
    block_builder,
    global_resource_message_id,
    group_by_resource,
    index_block,
    INDEX_RESOURCE_MESSAGE_ID,
    is_global_resource_message,
    is_index_resource_message,
    is_local_resource_message,
    local_resource_message_id,
    owning_resource_id,
    resource_deltas,
)
from ..prompts import (
    LEARN_MODE_GUIDE,
    LEARN_MODE_REMINDER,
    REVIEW_MODE_GUIDE,
    REVIEW_MODE_REMINDER,
    render_tool_allowlist,
)

if TYPE_CHECKING:
    from ..context import RootAgentContext


_logger = get_logger("agent.engine.root")


# ========================================================================================================================
# BRANCH MARKERS
# ========================================================================================================================

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


# ========================================================================================================================
# MODE GUIDES
# ========================================================================================================================

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


# ========================================================================================================================
# RECLAMATION STATUS REMINDER
# ========================================================================================================================

RECLAMATION_STATUS_MESSAGE_ID = "reclamation-status"
"""Fixed id for the derived staged-cleanup reminder (built fresh each ``prepare``, never persisted)."""

APP_CONTEXT_MESSAGE_ID = "app-context"
"""Fixed id for the derived app-context block — the app-published environment facts folded into one tail
``<system-reminder>`` (built fresh each ``prepare`` from ``ctx.app_context_hooks``, never persisted)."""


# ========================================================================================================================
# ROOT ENGINE
# ========================================================================================================================


class RootPromptEngine(PromptEngine["RootAgentContext"]):
    """Prompt engine for the root conversation agent.

    ``compile`` persists the durable facts — resource context messages and their consumed snapshot, the
    vector-index reminder and its snapshot, branch markers — and ``prepare`` arranges the floating blocks
    for each request and places the Anthropic cache-control breakpoints over the result. Implemented:
    global and local resource context, the index reminder, branch markers, modes, breakpoint placement, and
    semi-permanent reclamation (the machinery lives on ``PromptEngine``; the ``semi_perm`` breakpoint, the
    auto-tagger whitelist, the live ``AutoCompact`` gating, and the derived staged-cleanup reminder are this
    engine's wiring). Still roadmap: the index rendered as a derived message (the reminder already is one).

    Target prompt layout (positions assigned by tag, arranged per-request by ``prepare``):

        [system prompt]              rides on request.system_message; not cached on its own (~10k, cheap)
        [global resources]           pin: head
        ── breakpoint: head ──       only when global resources exist (protect the large pinned block)
        [inherited segments]         lineage, partitioned by one branch marker per ancestor node
        ── breakpoint: branch_up ──  ancestor fork points, nearest-first (fills leftover budget)
        ── breakpoint: branch_leaf ─ IMMEDIATELY BEFORE the leaf marker (the line below)
        [leaf branch marker]         node-specific id + content -> kept OUT of the cross-branch prefix
        [local resources]            pin: branch (this node's boundary, after the marker)
        [leaf segment]
        ── breakpoint: semi_perm ──  before the first semi-permanent message (reclamation reprices below it)
        [semi-permanent span]
        ── breakpoint: before_tail ─ always (the floor; excludes the volatile tail)
        [tail / pin: tail]           derived ephemerals: index reminder, status, volatile reminders

    Breakpoint priority & budget — a heuristic for user intent, made legible in the code by ``prepare``'s
    ordered candidate list and ``cache.allocate``'s integer budget. Anthropic caps a request at four
    breakpoints and reads the longest still-matching cached prefix, so the whole question is *which
    boundaries earn the scarce slots*. Candidates are tried in this priority order, highest first, until
    the budget runs out (the lowest applicable one is the first dropped):

        1. before_tail — always; the floor. The settled conversation grows behind it; only the volatile
           ephemeral tail reprices each turn.
        2. head — only when global resources exist; sits AFTER them, so a large pinned article is the thing
           protected. System+tools are not worth an isolated slot — they ride into the next boundary down.
        3. branch_leaf — the message immediately before THIS node's branch marker. The marker is
           node-specific, so it must stay OUT of any cross-branch prefix; ending the prefix just before it
           is what stays byte-identical across siblings and survives local-resource churn (local resources
           float to AFTER the marker, so they fall under before_tail, not here).
        4. branch_up — the message before each ANCESTOR branch marker, nearest-first, filling whatever budget
           is left: cousin+ warmth beyond branch_leaf's siblings (see ``_bp_branch_up``).
        5. semi_perm — before the first semi-permanent message, so reclamation (which rewrites only
           semi-permanent messages) reprices from there down, never the settled prefix above. Resolves to
           nothing until something is tagged semi-permanent, so it costs no slot on a thread that never
           reclaims.

    Each placed breakpoint carries its own TTL, set by ``prompt_cache_ttl``: an explicit ``5m`` / ``1h``
    applies uniformly, while ``dynamic`` puts 5m on the volatile ``before_tail`` floor (rewritten every
    turn) and 1h on the stable prefix anchors (written rarely, read every turn) — see ``_breakpoint_ttls``.

    The index reminder is (roadmap) a *derived* message: ``compile`` still calls ``resource_index.consume()``
    (a real advance of the live store), but the "what's queryable" block is rendered in ``prepare`` from the
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

    def __init__(self, *args, cache_supported: bool = False,
                 prompt_cache: OptionReader | None = None,
                 prompt_cache_ttl: OptionReader | None = None,
                 local_resource_placement: OptionReader | None = None,
                 auto_compact: OptionReader | None = None,
                 auto_compact_after: OptionReader | None = None,
                 auto_compact_threshold: OptionReader | None = None,
                 summarizer_key: str | None = None,
                 debug: bool = False, **kwargs) -> None:
        """Adds prompt-cache, layout, and reclamation configuration to the base engine; all other arguments
        forward to ``PromptEngine``. ``cache_supported`` is the provider gate, baked at build time (the
        runtime rebuilds the engine on a provider change, so a snapshot bool is correct — Anthropic places
        breakpoints, other providers cannot). ``prompt_cache`` / ``prompt_cache_ttl`` /
        ``local_resource_placement`` and the ``auto_compact`` trio (the master toggle, the expiry age, and
        the size threshold) are live option handles read fresh on each compile/prepare, so flipping any of
        them takes effect without a rebuild — and toggling ``auto_compact`` neither rebuilds nor disturbs the
        cached prefix (it only gates compile-side tagging/cleanup and the tail reminder). The ``after`` /
        ``threshold`` handles read as ints. ``summarizer_key`` names the subagent the ``summarize`` cleanup
        strategy provisions off the runtime (``None`` falls back to stub). ``debug`` gates the per-``prepare``
        prompt/usage dumps."""
        super().__init__(*args, **kwargs)
        self._cache_supported = cache_supported
        self._prompt_cache = prompt_cache
        self._prompt_cache_ttl = prompt_cache_ttl
        self._local_resource_placement = local_resource_placement
        self._auto_compact = auto_compact
        self._auto_compact_after = auto_compact_after
        self._auto_compact_threshold = auto_compact_threshold
        self._debug = debug
        self._summarizer_key = summarizer_key   # subagent key for the ``summarize`` strategy (None → stub)

    # ----- reclamation policy (live options override the base constants) ---- #

    def _reclaim_on(self) -> bool:
        return self._auto_compact is None or self._auto_compact.get() == "enabled"

    def _effective_threshold(self) -> int:
        ref = self._auto_compact_threshold
        return ref.get() if ref is not None else super()._effective_threshold()

    def _effective_expiry(self) -> int | None:
        ref = self._auto_compact_after
        return ref.get() if ref is not None else super()._effective_expiry()

    # ----- summarize strategy (provision a summarizer subagent off the runtime) ----- #

    def _summarizer(self, ctx: "RootAgentContext | None") -> Summarizer | None:
        """Override the base hook: summarize ``summarize``-strategy reclamations via a summarizer subagent.
        ``None`` (→ stub) unless a summarizer key is configured AND a runtime is wired — so offline / keyless
        runs, and any engine built without the key, stub instead."""
        if ctx is None or ctx.runtime is None or self._summarizer_key is None:
            return None
        return lambda targets: self._summarize_batch(ctx, targets)

    async def _summarize_batch(self, ctx: "RootAgentContext", targets: list[BaseMessage]) -> dict[str, str]:
        """Summarize each target concurrently (one fresh one-shot session apiece), bracketing the slow work
        with compaction events on this node's channel. Returns ``{id: summary}``; a target whose summary
        fails is omitted, so ``apply_cleanup`` stubs it instead — a failed summarizer never derails the run."""
        events = ctx.engine_events
        if events is not None:
            events.compaction_started(len(targets))
        try:
            results = await asyncio.gather(
                *(self._summarize_one(ctx, m) for m in targets), return_exceptions=True
            )
        finally:
            if events is not None:
                events.compaction_finished()
        summaries: dict[str, str] = {}
        for message, result in zip(targets, results):
            if isinstance(result, str) and result.strip():
                summaries[message.id] = result
            else:
                _logger.warning("summarize failed for %s; stubbing instead (%r)", message.id, result)
        return summaries

    async def _summarize_one(self, ctx: "RootAgentContext", message: BaseMessage) -> str:
        """Run one fresh summarizer session over ``message``'s content and return the summary text."""
        session = ctx.runtime.new(self._summarizer_key)
        result = await session.invoke(
            [MessagePayload(data=self._summary_input(message), role=MessagePayload.Role.USER)]
        )
        if result.response is None:
            raise ValueError("summarizer returned no response")
        content = result.response.content
        return content if isinstance(content, str) else str(content)

    @staticmethod
    def _summary_input(message: BaseMessage) -> str:
        """The text handed to the summarizer: the tool's name (for relevance) then its result content."""
        body = message.content if isinstance(message.content, str) else str(message.content)
        name = getattr(message, "name", None)
        return f"Tool: {name}\n\n{body}" if name else body

    async def compile(self, state: dict[str, Any], ctx: "RootAgentContext | None") -> dict[str, Any] | None:
        update: dict[str, Any] = {}

        # Repair patches lead — the adjacency contract (see PromptEngine.compile).
        patches = self.repair(state.get("messages", []))
        if patches:
            update["messages"] = list(patches)

        self._identify(state, update)              # stage 1 — base capability (see PromptEngine._identify)
        await self._cleanup(state, ctx, update)    # stage 2 — reclaim (stub/summarize)

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
          with no marker — the root — the body head); honored only under the ``leaf`` ``LocalResourcePlacement``.
          Under ``inline`` (the default) the branch pin is ignored here, so local resources keep their
          load-point position — see the option's help for the cache trade-off;
        - ``tail`` — the very end (the volatile region a breakpoint will sit before); a change there
          invalidates only the cheapest suffix, never the prefix up to the leaf.

        Floating keeps the prefix up to the leaf stable for the cache no matter where a block was loaded.
        Returns the request untouched when nothing is pinned."""
        # `inline` (the system default) keeps local resources at their load point; `leaf` floats them to the
        # branch boundary, so only a `leaf` placement pulls their `branch` pin into the float below. A
        # ref-less engine (no option wired — tests) falls back to the system default.
        ref = self._local_resource_placement
        float_branch = (ref.get() if ref is not None else "inline") == "leaf"
        buckets: dict[Pin, list[BaseMessage]] = {"head": [], "branch": [], "tail": []}
        inline: list[BaseMessage] = []
        for m in request.messages:
            anchor = pin_of(m)
            if anchor == "branch" and not float_branch:
                anchor = None
            (buckets[anchor] if anchor in buckets else inline).append(m)

        # Derived ephemeral tail blocks, rebuilt each prepare and never persisted (so their churn reprices
        # only the volatile suffix, never the cached prefix): the app-published context facts, then the
        # staged-cleanup reminder. Both built off this request's own messages / context.
        app_context = self._app_context_reminder(ctx)
        if app_context is not None:
            buckets["tail"].append(app_context)

        reminder = self._reclamation_reminder(request.messages)
        if reminder is not None:
            buckets["tail"].append(reminder)

        if not any(buckets.values()):
            result = request
        else:
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

            result = request.override(messages=[
                *system, *buckets["head"], *body[:split], *buckets["branch"], *body[split:], *buckets["tail"],
            ])

        result = self._place_breakpoints(result, ctx)

        # In debug mode, dump the wire request + a usage report per prepare (see engine.dump.PROMPT_DUMP_DIR).
        # The report is computed over the request's own messages, so its provider total comes from the latest
        # prior AIMessage — anchored one model call behind (the current-vs-last-call drift engine.usage
        # documents), harmless for an inspection dump.
        if self._debug:
            node = ctx.node_id if ctx is not None else None
            dump_request(result, node)
            dump_report(self.report({"messages": result.messages}), node)
        return result

    # ----- reclamation reminder -------------------------------------------- #

    def _reclamation_reminder(self, messages: list[BaseMessage]) -> BaseMessage | None:
        """The derived staged-cleanup reminder (a tail-pinned ``<system-reminder>``): per reclaimable group,
        its size and the soonest turn it auto-clears, pointing the agent at ``hydrate`` / ``cleanup_context``.
        Built fresh each ``prepare`` from the request's own messages, never persisted, and only when
        reclamation is on and something is staged — so toggling ``AutoCompact`` and the counts ticking down
        touch only the volatile tail, never the cached prefix. ``None`` when off or nothing is staged."""
        if not self._reclaim_on():
            return None
        status = reclamation_status(messages, self._effective_expiry())
        if not status:
            return None
        lines = []
        for group, (count, turns) in sorted(status.items()):
            when = "no expiry" if turns is None else "due now" if turns <= 0 else f"~{turns} turn(s) left"
            lines.append(f"- {group}: {count} message(s), {when}")
        body = (
            "Context auto-compaction is on. These earlier tool results are staged to be cleared to a short "
            "placeholder as they age out (you can re-run the tool to fetch one again). To keep a group in "
            "context longer, call hydrate(group); to clear one now, call cleanup_context(group).\n"
            + "\n".join(lines)
        )
        content = f"<system-reminder>\n{body}\n</system-reminder>"
        return pin(set_role(HumanMessage(content=content, id=RECLAMATION_STATUS_MESSAGE_ID), "system"), "tail")

    # ----- app-context reminder -------------------------------------------- #

    def _app_context_reminder(self, ctx: "RootAgentContext | None") -> BaseMessage | None:
        """The derived app-context block (a tail-pinned ``<system-reminder>``): the app-published environment
        facts (``ctx.app_context_hooks``) folded into one message, rebuilt fresh each ``prepare`` and never
        persisted — so a fact changing reprices only the volatile tail. ``None`` when no hooks are wired or
        every producer yields nothing this turn, so an empty registry touches neither the prompt nor the
        cache."""
        if ctx is None or ctx.app_context_hooks is None:
            return None
        fragments = ctx.app_context_hooks.fragments()
        if not fragments:
            return None
        content = "<system-reminder>\n" + "\n".join(fragments) + "\n</system-reminder>"
        return pin(set_role(HumanMessage(content=content, id=APP_CONTEXT_MESSAGE_ID), "system"), "tail")

    # ----- cache breakpoints ----------------------------------------------- #

    def _place_breakpoints(self, request: ModelRequest, ctx: "RootAgentContext | None") -> ModelRequest:
        """Place the cache-control breakpoints over the laid-out request — view-only, annotating COPIES so
        the shared state messages are never touched. Gated on the provider supporting breakpoints and the
        live ``prompt_cache`` toggle; the live ``prompt_cache_ttl`` sets the TTL per position (see
        ``_breakpoint_ttls``). The candidate list IS the priority order and ``allocate``'s budget IS the cap
        (see the class docstring). Returns the request unchanged when caching is off or nothing landed, so
        request identity is preserved."""
        if not (self._cache_supported and self._prompt_cache is not None
                and self._prompt_cache.get() == "enabled"):
            return request
        ttl = self._prompt_cache_ttl.get() if self._prompt_cache_ttl is not None else "5m"
        tail_cc, stable_cc = self._breakpoint_ttls(ttl)
        candidates = [
            Breakpoint("before_tail", self._bp_before_tail, tail_cc),
            Breakpoint("head", self._bp_head, stable_cc),
            Breakpoint("branch_leaf", lambda msgs: self._bp_branch_leaf(msgs, ctx), stable_cc),
            Breakpoint("branch_up", lambda msgs: self._bp_branch_up(msgs, ctx), stable_cc),
            Breakpoint("semi_perm", self._bp_semi_perm, stable_cc),
        ]
        messages = allocate(request.messages, candidates)
        return request if messages is request.messages else request.override(messages=messages)

    @staticmethod
    def _breakpoint_ttls(option: str) -> tuple[CacheControl, CacheControl]:
        """The ``(tail, stable-prefix)`` cache-control descriptors for a ``prompt_cache_ttl`` value. An
        explicit ``5m`` / ``1h`` applies uniformly; ``dynamic`` splits them — 5m on the volatile
        ``before_tail`` floor (rewritten every turn, so the cheaper 1.25x write is right) and 1h on the
        stable prefix anchors (written rarely, read every turn, so the 2x write amortizes and survives
        idle gaps)."""
        if option == "dynamic":
            return cache_control("5m"), cache_control("1h")
        cc = cache_control(option)
        return cc, cc

    @staticmethod
    def _bp_before_tail(messages: list[BaseMessage]) -> BaseMessage | None:
        """The floor (always tried first): the last message NOT floated to the tail, so the breakpoint
        falls just before the volatile ephemeral tail and only the tail reprices each turn."""
        return next((m for m in reversed(messages)
                     if pin_of(m) != "tail" and not isinstance(m, SystemMessage)), None)

    @staticmethod
    def _bp_head(messages: list[BaseMessage]) -> BaseMessage | None:
        """The head boundary, only when global resources exist: the last head-pinned message, so the
        breakpoint caches tools + system + the (large) global block as one stable, graph-wide prefix.
        ``None`` with no global resources — system+tools alone aren't worth an isolated slot."""
        return next((m for m in reversed(messages) if pin_of(m) == "head"), None)

    @staticmethod
    def _before_index(messages: list[BaseMessage], index: int) -> BaseMessage | None:
        """The latest cache-annotatable message strictly before position ``index`` — the breakpoint target
        that ends a stable prefix JUST before some boundary message (a node-specific branch marker, or the
        first semi-permanent message), keeping that message and everything after it out of the
        shared/stable prefix. Skips back over messages with no annotatable content (the block-less tool-use
        ``AIMessage`` that precedes its tool result is the common case — a breakpoint can't ride an empty
        block, so it would otherwise be dropped and the prefix left unprotected). ``None`` when nothing
        eligible precedes it (the boundary leads the body, or only a ``SystemMessage`` sits before it)."""
        i = index - 1
        while i >= 0 and not isinstance(messages[i], SystemMessage):
            if is_annotatable(messages[i]):
                return messages[i]
            i -= 1
        return None

    @staticmethod
    def _bp_branch_leaf(messages: list[BaseMessage], ctx: "RootAgentContext | None") -> BaseMessage | None:
        """The cross-branch stable boundary: the message IMMEDIATELY BEFORE this node's branch marker. The
        marker is node-specific (id + parent name), so ending the cached prefix just before it keeps that
        prefix byte-identical across sibling branches. ``None`` for the root / a graph-less session (no
        marker), or when nothing precedes the marker.

        Local resources interact with this cut via ``LocalResourcePlacement``: under ``leaf`` they float to
        AFTER the marker, so they sit below this prefix and churn never touches it; under ``inline`` (the
        default) an INHERITED resource stays in its ancestor segment — inside this prefix, cached across
        branch switches, at the cost of breaking the prefix when that resource churns."""
        if ctx is None or ctx.node_id is None:
            return None
        marker_id = branch_marker_message_id(ctx.node_id)
        index = next((i for i, m in enumerate(messages) if m.id == marker_id), None)
        return None if index is None else RootPromptEngine._before_index(messages, index)

    @staticmethod
    def _bp_branch_up(messages: list[BaseMessage], ctx: "RootAgentContext | None") -> list[BaseMessage]:
        """Ancestor fork points higher in the lineage: the message immediately before each ANCESTOR branch
        marker (every ``branch-marker-*`` except this node's own). Each such boundary is a prefix shared
        with a WIDER set of relatives than ``branch_leaf``'s — before the parent's marker is the prefix all
        cousins share, before the grandparent's a shorter prefix shared more widely, and so on: a graduated
        staircase of fallback read points between ``head`` and ``branch_leaf``. So this earns its low-priority
        slots only when navigation actually jumps between distant subtrees (cousin+), which ``branch_leaf``
        does NOT already cover (it covers siblings).

        Policy (first pass): NEAREST-first — closest ancestor to the leaf, then upward — so the scarce
        leftover slots cache the most lineage and cover the most common (nearest) switches. ``allocate``
        takes them greedily until the budget runs out; each rides the same stable TTL the candidate carries
        (1h under ``dynamic``).

        Worth playing with later (presumes more budget than we have): vary the TTL by ancestor DEPTH — e.g.
        a 1h block at a deep near-root ancestor (rarely invalidated) vs. a 5m block at the grandparent, or
        alternating root-ward / leaf-ward placements — which would want per-target descriptors rather than
        one ``cache_control`` per candidate."""
        if ctx is None or ctx.node_id is None:
            return []
        own = branch_marker_message_id(ctx.node_id)
        ancestor_markers = [
            i for i, m in enumerate(messages)
            if (m.id or "").startswith(BRANCH_MARKER_PREFIX) and m.id != own
        ]
        return [
            target for i in reversed(ancestor_markers)            # nearest (highest index) first
            if (target := RootPromptEngine._before_index(messages, i)) is not None
        ]

    @staticmethod
    def _bp_semi_perm(messages: list[BaseMessage]) -> BaseMessage | None:
        """The message before the first semi-permanent one. Reclamation only ever rewrites semi-permanent
        messages (in place, to a stub), so ending a cached prefix just before the span means a reclamation
        reprices from there down and never the settled prefix above. As the front of the span gets reclaimed
        (each stub promotes to ``permanent``), the boundary advances and those stubs join the stable prefix.
        ``None`` until something is tagged semi-permanent, or when a semi-permanent message leads the body."""
        index = next((i for i, m in enumerate(messages) if lifetime_of(m) == "semi-permanent"), None)
        return None if index is None else RootPromptEngine._before_index(messages, index)

    # ----- usage classification -------------------------------------------- #

    def _message_kind(self, message: BaseMessage) -> str:
        """Refine the generic classification (see ``PromptEngine._message_kind``) by recognizing the root
        engine's own well-known message ids, so the usage breakdown distinguishes the global vs local
        resource-context channels, the vector-index reminder, mode guides, and branch markers from ordinary
        conversation."""
        if is_index_resource_message(message):
            return "resource_index"
        if is_global_resource_message(message):
            return "global_resource"
        if is_local_resource_message(message):
            return "local_resource"
        mid = message.id or ""
        if mid == RECLAMATION_STATUS_MESSAGE_ID:
            return "reclamation_status"
        if mid == APP_CONTEXT_MESSAGE_ID:
            return "app_context"
        if mid.startswith(MODE_GUIDE_PREFIX):
            return "guide"
        if mid.startswith(BRANCH_MARKER_PREFIX):
            return "branch_marker"
        return super()._message_kind(message)

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
            rid = owning_resource_id(node, tree)
            if rid is not None:
                touched.add(rid)

        # Regroup the channel's desired nodes by owning resource — only for the touched ones.
        current: dict[int, list[ResourceTreeNode]] = {rid: [] for rid in touched}
        for node in desired:
            rid = owning_resource_id(node, tree)
            if rid in current:
                current[rid].append(node)

        messages: list[BaseMessage] = []
        for rid in sorted(touched):
            nodes = current[rid]
            mid = message_id(rid)
            if not nodes:
                messages.append(RemoveMessage(id=mid))
                continue
            content = await store.block(rid, block_builder(session_factory, rid, nodes))
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

        grouped = group_by_resource(loaded, ctx.resource_index.tree)
        content = await index_block(grouped, ctx.session_factory) if grouped else None
        if content is not None:
            update.setdefault("messages", []).append(
                pin(HumanMessage(content=content, id=INDEX_RESOURCE_MESSAGE_ID), "tail")
            )
        elif consumed:
            # The reminder existed and now has nothing to announce -> drop it. Guarded on a non-empty
            # prior snapshot: a RemoveMessage for an id that was never injected would raise.
            update.setdefault("messages", []).append(RemoveMessage(id=INDEX_RESOURCE_MESSAGE_ID))
        update["consumed_resource_index"] = list(loaded)

    # ----- mode pass ------------------------------------------------------- #

    def _compile_mode(
        self, state: dict[str, Any], ctx: "RootAgentContext", update: dict[str, Any]
    ) -> None:
        """Witness a mode switch on the ``LocalAppContextStore`` (the SSOT) and react.

        ``app_state.mode`` carries desire (the user via the view, or the agent via ``set_mode``);
        ``RootAgentState["mode"]`` is the engine-committed fact. They match in steady state, so the diff is
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
