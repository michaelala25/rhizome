"""``RootPromptEngine``: the prompt engine for the root conversation agent.

Builds on ``engine.base.PromptEngine`` (the compile/prepare/repair contract and the message-lifetime
machinery) and ``engine.resources`` (the resource-context helpers). ``compile`` adds the root agent's
durable facts — global/local resource context blocks, the vector-index reminder, branch markers, mode
guides — and ``prepare`` arranges the floating blocks for each wire request.

The well-known message ids for resource/index blocks live in ``engine.resources``; the branch-marker and
mode-guide id schemes live here, beside the engine that owns them.
"""

from typing import Any, Callable, TYPE_CHECKING

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage

from rhizome.resources_new import ResourceContextStore, ResourceLoadDelta, ResourceTreeNode

from .base import ensure_message_id, ingest_payloads, PromptEngine
from .cleanup import promote
from .metadata import lifetime_of, pin, Pin, pin_of
from .resources import (
    block_builder,
    global_resource_message_id,
    group_by_resource,
    index_block,
    INDEX_RESOURCE_MESSAGE_ID,
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
# ROOT ENGINE
# ========================================================================================================================


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
        """Witness a mode switch on the ``AppContextStore`` (the SSOT) and react.

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
