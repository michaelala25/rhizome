"""Prompt-engine compile/prepare primitives: id minting, payload ingestion, orphan repair, resource
deltas, and the persist-vs-view split (``compile`` lands in state, ``prepare`` shapes one request only).

Engines now receive the full agent context (a ``RootAgentContext``) at compile/prepare — the channels the
old ``PromptCompileContext`` carried (``pending``, ``local_resources``, ``global_resources``) live on it.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.agent_new.app_context import AppContextStore
from rhizome.agent_new.cleanup import mark_reclaimable
from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.metadata import lifetime_of, meta, pin, pin_of, role_of, set_lifetime, set_role
from rhizome.agent_new.payload import MessagePayload, PayloadQueue, StateUpdatePayload
from rhizome.agent_new.prompt_engine import (
    branch_marker_message_id,
    global_resource_message_id,
    INDEX_RESOURCE_MESSAGE_ID,
    ingest_payloads,
    is_global_resource_message,
    is_index_resource_message,
    local_resource_message_id,
    mode_guide_message_id,
    patch_orphaned_tool_calls,
    payload_message,
    PromptEngine,
    resource_deltas,
    RootPromptEngine,
)
from rhizome.agent_new.topology import NodeInfo, TopologySnapshot, TopologyView
from rhizome.db.models import Base, Resource, ResourceContent, ResourceSection
from rhizome.resources_new import ResourceContextStore, ResourceIndexStore, ResourceTree, ResourceTreeNode

from .fakes import ai_contents, drive, EchoModel, make_build, make_runtime, register, user

MARKER = "[ephemeral-marker]"


def _tool_call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": call_id}])


# ------------------------------------------------------------------------------------------------
# Message construction & payload ingestion
# ------------------------------------------------------------------------------------------------

def test_payload_messages_are_minted_with_ids():
    for role in MessagePayload.Role:
        message = payload_message(MessagePayload(data="x", role=role))
        assert message.id is not None and len(message.id) == 36   # a uuid4

    system = payload_message(MessagePayload(data="note", role=MessagePayload.Role.SYSTEM))
    assert isinstance(system, HumanMessage) and "<system>" in system.content


def test_ingest_payloads_merges_state_updates():
    update: dict = {}
    ingest_payloads(
        [
            user("hello"),
            StateUpdatePayload(data={"mode": "learn", "messages": [AIMessage(content="synthetic")]}),
            StateUpdatePayload(data={"mode": "review"}),     # last-writer-wins within the batch
        ],
        update,
    )
    assert update["mode"] == "review"
    assert [type(m).__name__ for m in update["messages"]] == ["HumanMessage", "AIMessage"]


# ------------------------------------------------------------------------------------------------
# Orphan repair
# ------------------------------------------------------------------------------------------------

def test_patch_orphaned_tool_calls_is_idempotent_and_targets_latest():
    torn = [_tool_call("tc-1")]
    patches = patch_orphaned_tool_calls(torn, reason="cancelled")
    assert len(patches) == 1 and patches[0].tool_call_id == "tc-1" and patches[0].id is not None

    # Already-answered calls produce no patches; only the most recent AIMessage is inspected.
    assert patch_orphaned_tool_calls(torn + patches, reason="cancelled") == []
    older_then_answered = [
        _tool_call("tc-old"),
        ToolMessage(content="ok", tool_call_id="tc-old"),
        AIMessage(content="done"),
    ]
    assert patch_orphaned_tool_calls(older_then_answered, reason="cancelled") == []


async def test_compile_orders_patches_before_ingested_payloads():
    """Load-bearing ordering: the repair patch lands adjacent to the dangling tool_use, ahead of any new
    payload messages in the same update (the Anthropic adjacency contract)."""
    engine = PromptEngine()
    queue = PayloadQueue()
    queue.post(user("next turn"))

    update = await engine.compile({"messages": [_tool_call("tc-2")]}, RootAgentContext(pending=queue))
    assert [type(m).__name__ for m in update["messages"]] == ["ToolMessage", "HumanMessage"]
    assert not queue, "queue drained by compile"


async def test_compile_without_context_still_repairs_and_is_idempotent():
    engine = PromptEngine()
    update = await engine.compile({"messages": [_tool_call("tc-3")]}, None)
    assert [m.tool_call_id for m in update["messages"]] == ["tc-3"]
    # Nothing to repair, no payloads -> no update at all (the idempotence engines rely on).
    assert await engine.compile({"messages": [HumanMessage(content="x")]}, None) is None


# ------------------------------------------------------------------------------------------------
# prepare shapes the request, never state
# ------------------------------------------------------------------------------------------------

class MarkerEngine(PromptEngine):
    """Appends an ephemeral marker to the outgoing REQUEST only — never to state. Uses
    ``request.override(...)`` per the prepare contract (attribute assignment is deprecated upstream;
    both are view-only — the model node persists only the model's output)."""

    async def prepare(self, request, ctx):
        return request.override(messages=[*request.messages, HumanMessage(content=MARKER)])


async def test_prepare_shapes_the_request_but_never_state():
    """The persist/view split, mechanically: the model demonstrably SAW the marker (its echo quotes it),
    yet no marker message exists in the checkpointed state."""
    h = make_runtime()
    register(h.runtime, "root", make_build(EchoModel, engine_factory=MarkerEngine))
    session = h.runtime.new("root")

    await drive(session, user("hello"))
    state = await session.agent_state

    # The echo model replies with the last human it saw — which was the appended marker.
    assert ai_contents(state)[-1] == f"echo:{MARKER}|seen:2"
    # Yet no marker MESSAGE persisted: exactly the user turn and the reply, nothing between. (The marker
    # substring legitimately appears inside the AI reply — it quotes what it saw — so this is structural.)
    assert [type(m).__name__ for m in state["messages"]] == ["HumanMessage", "AIMessage"]
    assert state["messages"][0].content == "hello"


# ------------------------------------------------------------------------------------------------
# Resource deltas  (kept for now; likely relocates with the resource layer later)
# ------------------------------------------------------------------------------------------------

def _make_tree() -> ResourceTree:
    # Resource 1 with three sections so partial loads don't promote the parent.
    tree = ResourceTree()
    tree.load_rows([1], [(10, 1, None), (11, 1, None), (12, 1, None)])
    return tree


def test_resource_deltas_full_lifecycle():
    tree = _make_tree()
    r1 = ResourceTreeNode("resource", 1)
    s10, s11 = ResourceTreeNode("section", 10), ResourceTreeNode("section", 11)

    global_store, local_store = ResourceContextStore(tree), ResourceContextStore(tree)
    global_store.set_loaded(s10, True)
    local_store.set_loaded(s10, True)   # overlaps global: the backstop suppresses it
    local_store.set_loaded(s11, True)
    ctx = RootAgentContext(local_resources=local_store, global_resources=global_store)

    # First consumption: s10 injects globally; locally only s11 (suppressed s10 is NOT recorded).
    global_delta, local_delta, snapshot = resource_deltas(None, ctx)
    assert global_delta.additions == [s10] and not global_delta.removals
    assert local_delta.additions == [s11] and not local_delta.removals
    assert snapshot == {"global": [s10], "local": [s11]}

    # Steady state round-trips to empty deltas.
    global_delta, local_delta, snapshot2 = resource_deltas(snapshot, ctx)
    assert not global_delta and not local_delta and snapshot2 == snapshot

    # Self-heal: global unloads s10 -> global removal AND the local suppression releases.
    global_store.set_loaded(s10, False)
    global_delta, local_delta, snapshot3 = resource_deltas(snapshot2, ctx)
    assert global_delta.removals == [s10]
    assert local_delta.additions == [s10]
    assert set(snapshot3["local"]) == {s10, s11}

    # Ancestor coverage: global loads the whole resource -> local fully suppressed via walk-up.
    global_store.set_loaded(r1, True)
    global_delta, local_delta, snapshot4 = resource_deltas(snapshot3, ctx)
    assert global_delta.additions == [r1]
    assert set(local_delta.removals) == {s10, s11} and snapshot4["local"] == []


def test_resource_deltas_without_stores():
    global_delta, local_delta, snapshot = resource_deltas(None, RootAgentContext())
    assert not global_delta and not local_delta
    assert snapshot == {"global": [], "local": []}


# ------------------------------------------------------------------------------------------------
# Root engine — prepare lifts the global block to a stable prefix (view-only)
# ------------------------------------------------------------------------------------------------

class _Request:
    """Minimal ModelRequest stand-in: prepare only reads ``.messages`` and calls ``.override``."""

    def __init__(self, messages):
        self.messages = messages

    def override(self, *, messages):
        return _Request(messages)


async def test_prepare_lifts_global_resources_after_system():
    engine = RootPromptEngine()
    # Tagged pin=head and sitting mid-body — where it would land if loaded partway through the
    # conversation; prepare floats it to just after the system message, identity and tag intact.
    resource = pin(HumanMessage(content="RES", id=global_resource_message_id(1)), "head")
    request = _Request([
        SystemMessage(content="sys"),
        HumanMessage(content="u1"),
        AIMessage(content="a1"),
        resource,
        HumanMessage(content="u2"),
    ])
    out = await engine.prepare(request, None)
    assert [m.content for m in out.messages] == ["sys", "RES", "u1", "a1", "u2"]
    assert is_global_resource_message(out.messages[1]) and pin_of(out.messages[1]) == "head"


async def test_prepare_is_noop_without_resources():
    engine = RootPromptEngine()
    request = _Request([SystemMessage(content="sys"), HumanMessage(content="u1")])
    assert await engine.prepare(request, None) is request   # unchanged object, no reshuffle


async def test_prepare_floats_index_to_tail():
    engine = RootPromptEngine()
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    idx = pin(HumanMessage(content="IDX", id=INDEX_RESOURCE_MESSAGE_ID), "tail")
    # Both sit mid-body (loaded partway through); the head pin lifts to just after the system message,
    # the tail pin floats to the very end.
    request = _Request([
        SystemMessage(content="sys"),
        idx,
        HumanMessage(content="u1"),
        g,
        HumanMessage(content="u2"),
    ])
    out = await engine.prepare(request, None)
    assert [m.content for m in out.messages] == ["sys", "G", "u1", "u2", "IDX"]
    assert is_index_resource_message(out.messages[-1])


# ------------------------------------------------------------------------------------------------
# Root engine — compile persists per-resource messages + the consumed snapshot
# ------------------------------------------------------------------------------------------------

@pytest.fixture
async def resource_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Resource(id=1, name="Doc"))
        await s.flush()
        s.add(ResourceContent(resource_id=1, raw_text="HELLO WORLD"))
        # Two sections so loading one stays section-level (a lone child would promote to the resource).
        s.add_all([
            ResourceSection(id=11, resource_id=1, title="Intro", depth=0, position=0, start_offset=0),
            ResourceSection(id=12, resource_id=1, title="Body", depth=0, position=1, start_offset=6),
        ])
        await s.commit()
    yield factory
    await engine.dispose()


async def test_compile_injects_then_removes_global_resource(resource_db):
    tree = ResourceTree(resource_db)
    await tree.refresh()
    store = ResourceContextStore(tree, cache=True)
    store.set_loaded(ResourceTreeNode("section", 11), True)
    ctx = RootAgentContext(global_resources=store, session_factory=resource_db)
    engine = RootPromptEngine()

    # First load: one HumanMessage keyed by resource id, carrying the section content; snapshot records it.
    update = await engine.compile({"messages": []}, ctx)
    resource_msgs = [m for m in update["messages"] if is_global_resource_message(m)]
    assert len(resource_msgs) == 1 and resource_msgs[0].id == global_resource_message_id(1)
    assert pin_of(resource_msgs[0]) == "head"    # tagged for prepare's head anchor at construction
    assert "HELLO" in resource_msgs[0].content   # section 11's slice, [0:6)
    assert update["consumed_resource_context"]["global"] == [ResourceTreeNode("section", 11)]

    # Steady state: consumed matches desired -> no update at all.
    consumed = {"messages": [], "consumed_resource_context": update["consumed_resource_context"]}
    assert await engine.compile(consumed, ctx) is None

    # Unload: RemoveMessage under the same id, snapshot empties.
    store.set_loaded(ResourceTreeNode("section", 11), False)
    update2 = await engine.compile(consumed, ctx)
    removals = [m for m in update2["messages"] if isinstance(m, RemoveMessage)]
    assert len(removals) == 1 and removals[0].id == global_resource_message_id(1)
    assert update2["consumed_resource_context"]["global"] == []


# ------------------------------------------------------------------------------------------------
# Root engine — local (per-node) resources: compile, concomitance with global, prepare placement
# ------------------------------------------------------------------------------------------------

async def test_compile_injects_then_removes_local_resource(resource_db):
    tree = ResourceTree(resource_db)
    await tree.refresh()
    local = ResourceContextStore(tree)               # node-local: uncached by default
    local.set_loaded(ResourceTreeNode("section", 11), True)
    ctx = RootAgentContext(local_resources=local, session_factory=resource_db)
    engine = RootPromptEngine()

    update = await engine.compile({"messages": []}, ctx)
    locals_ = [m for m in update["messages"] if m.id == local_resource_message_id(1)]
    assert len(locals_) == 1 and "HELLO" in locals_[0].content
    assert pin_of(locals_[0]) == "branch"        # tagged for prepare's branch anchor at construction
    assert update["consumed_resource_context"]["local"] == [ResourceTreeNode("section", 11)]
    assert update["consumed_resource_context"]["global"] == []

    consumed = {"messages": [], "consumed_resource_context": update["consumed_resource_context"]}
    assert await engine.compile(consumed, ctx) is None   # steady state

    local.set_loaded(ResourceTreeNode("section", 11), False)
    update2 = await engine.compile(consumed, ctx)
    removals = [m for m in update2["messages"] if isinstance(m, RemoveMessage)]
    assert len(removals) == 1 and removals[0].id == local_resource_message_id(1)
    assert update2["consumed_resource_context"]["local"] == []


async def test_compile_global_coverage_suppresses_local(resource_db):
    """Concomitance: a resource the global store already covers is kept out of the local block, so only
    a global message is emitted — never a duplicate local one."""
    tree = ResourceTree(resource_db)
    await tree.refresh()
    global_store = ResourceContextStore(tree, cache=True)
    local = ResourceContextStore(tree)
    global_store.set_loaded(ResourceTreeNode("resource", 1), True)   # whole resource, globally
    local.set_loaded(ResourceTreeNode("section", 11), True)          # a section of it, locally
    ctx = RootAgentContext(global_resources=global_store, local_resources=local, session_factory=resource_db)

    update = await RootPromptEngine().compile({"messages": []}, ctx)
    assert [m.id for m in update["messages"]] == [global_resource_message_id(1)]   # only the global block
    assert update["consumed_resource_context"]["global"] == [ResourceTreeNode("resource", 1)]
    assert update["consumed_resource_context"]["local"] == []                                      # section 11 suppressed


async def test_prepare_positions_local_after_branch_marker():
    engine = RootPromptEngine()
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    marker = HumanMessage(content="<system>branched</system>", id=branch_marker_message_id(5))  # inline anchor
    loc = pin(HumanMessage(content="L", id=local_resource_message_id(2)), "branch")
    # State order: resources appended at the tail; the marker sits mid-body at the segment boundary.
    request = _Request([
        SystemMessage(content="sys"),
        HumanMessage(content="inherited"),
        marker,
        HumanMessage(content="leaf"),
        g,
        loc,
    ])
    out = await engine.prepare(request, RootAgentContext(node_id=5))
    # head pin after system; branch pin opens this node's segment, right after the marker.
    assert [m.content for m in out.messages] == \
        ["sys", "G", "inherited", "<system>branched</system>", "L", "leaf"]


async def test_prepare_positions_local_after_globals_for_root():
    engine = RootPromptEngine()
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    loc = pin(HumanMessage(content="L", id=local_resource_message_id(2)), "branch")
    request = _Request([SystemMessage(content="sys"), HumanMessage(content="conv"), g, loc])
    out = await engine.prepare(request, RootAgentContext(node_id=0))   # root: no branch marker
    assert [m.content for m in out.messages] == ["sys", "G", "L", "conv"]


# ------------------------------------------------------------------------------------------------
# Root engine — index reminder: lazy consume, single message, per-thread snapshot
# ------------------------------------------------------------------------------------------------

async def test_compile_injects_then_removes_index_reminder(resource_db):
    tree = ResourceTree(resource_db)
    await tree.refresh()
    index = ResourceIndexStore(tree)
    index.set_loaded(ResourceTreeNode("section", 11), True)
    ctx = RootAgentContext(resource_index=index, session_factory=resource_db)
    engine = RootPromptEngine()

    # First compile: one stable-id reminder listing the indexed section; snapshot records the loaded set.
    update = await engine.compile({"messages": []}, ctx)
    reminders = [m for m in update["messages"] if is_index_resource_message(m)]
    assert len(reminders) == 1 and reminders[0].id == INDEX_RESOURCE_MESSAGE_ID
    assert pin_of(reminders[0]) == "tail"        # tagged for prepare's tail anchor at construction
    assert "<system>" in reminders[0].content and "Doc" in reminders[0].content
    assert "Intro" in reminders[0].content                       # section 11's title (partial -> nested)
    assert update["consumed_resource_index"] == [ResourceTreeNode("section", 11)]
    # consume() ran during compile: the graph-global watermark advanced (lazy ingest on first use).
    assert index.consumed == index.loaded

    # Steady state: snapshot matches the loaded set -> no update at all.
    consumed = {"messages": [], "consumed_resource_index": update["consumed_resource_index"]}
    assert await engine.compile(consumed, ctx) is None

    # Unload: the reminder is removed under the same id and the snapshot empties.
    index.set_loaded(ResourceTreeNode("section", 11), False)
    update2 = await engine.compile(consumed, ctx)
    removals = [m for m in update2["messages"] if isinstance(m, RemoveMessage)]
    assert len(removals) == 1 and removals[0].id == INDEX_RESOURCE_MESSAGE_ID
    assert update2["consumed_resource_index"] == []


async def test_index_consume_is_graph_global_but_reminder_is_per_thread(resource_db):
    """Ingestion is graph-global (the watermark advances once), but each thread's reminder tracks its
    OWN snapshot — a second thread with no snapshot still emits its reminder."""
    tree = ResourceTree(resource_db)
    await tree.refresh()
    index = ResourceIndexStore(tree)
    index.set_loaded(ResourceTreeNode("resource", 1), True)        # whole resource -> name-only listing
    ctx = RootAgentContext(resource_index=index, session_factory=resource_db)
    engine = RootPromptEngine()

    # Thread A: first compile populates the index and emits A's reminder.
    a = await engine.compile({"messages": []}, ctx)
    assert any(is_index_resource_message(m) for m in a["messages"])
    assert index.consumed == {ResourceTreeNode("resource", 1)}    # graph-global watermark advanced

    # Thread B (fresh, empty snapshot): still emits ITS reminder even though the index is already warm.
    b = await engine.compile({"messages": []}, ctx)
    assert any(is_index_resource_message(m) for m in b["messages"])
    assert b["consumed_resource_index"] == [ResourceTreeNode("resource", 1)]


async def test_compile_no_index_pass_without_store(resource_db):
    # No index store on the context -> the index pass is inert (no message, no snapshot key).
    ctx = RootAgentContext(session_factory=resource_db)
    assert await RootPromptEngine().compile({"messages": []}, ctx) is None


# ------------------------------------------------------------------------------------------------
# Root engine — branch marker, witnessed from the topology (pull, no agent state)
# ------------------------------------------------------------------------------------------------

def _topology(node_id: int, *, parent_id: int | None = None, parent_name: str | None = None) -> TopologyView:
    """A one- or two-node topology view: ``node_id``, optionally parented at ``parent_id``."""
    nodes = {node_id: NodeInfo(id=node_id, parents=(parent_id,) if parent_id is not None else ())}
    if parent_id is not None:
        nodes[parent_id] = NodeInfo(id=parent_id, children=(node_id,), frozen=True, name=parent_name)
    view = TopologyView()
    view.publish(TopologySnapshot(nodes=nodes))
    return view


async def test_compile_injects_branch_marker_once():
    engine = RootPromptEngine()
    ctx = RootAgentContext(topology=_topology(1, parent_id=0, parent_name="main"), node_id=1)

    update = await engine.compile({"messages": []}, ctx)
    markers = [m for m in update["messages"] if m.id == branch_marker_message_id(1)]
    assert len(markers) == 1
    assert "<system>" in markers[0].content and "main" in markers[0].content

    # Idempotent: with the marker already in state, the next compile produces nothing.
    assert await engine.compile({"messages": update["messages"]}, ctx) is None


async def test_compile_no_branch_marker_for_root():
    engine = RootPromptEngine()
    ctx = RootAgentContext(topology=_topology(0), node_id=0)   # root: no parents
    assert await engine.compile({"messages": []}, ctx) is None


async def test_compile_no_branch_marker_without_topology():
    engine = RootPromptEngine()
    # A graph-less session carries no topology handle -> no marker, ever.
    assert await engine.compile({"messages": []}, RootAgentContext(node_id=1)) is None


# ------------------------------------------------------------------------------------------------
# Root engine — mode switches: witnessed from the AppContextStore (SSOT), narrated in compile
# ------------------------------------------------------------------------------------------------

async def test_compile_first_entry_injects_full_guide_and_allowlist():
    engine = RootPromptEngine()
    ctx = RootAgentContext(app_state=AppContextStore(mode="learn"))

    # Fresh thread (mode baseline idle) -> learn: the full guide under the deterministic guide id.
    update = await engine.compile({"messages": []}, ctx)
    assert update["mode"] == "learn"
    guides = [m for m in update["messages"] if m.id == mode_guide_message_id("learn")]
    assert len(guides) == 1
    content = guides[0].content
    assert content.startswith("<system>") and "Guide: Learn Mode" in content
    assert "Tools permitted in **learn** mode" in content   # allowlist in the same block


async def test_compile_reentry_injects_concise_reminder():
    engine = RootPromptEngine()
    ctx = RootAgentContext(app_state=AppContextStore(mode="learn"))
    # The learn guide is already in context and the committed mode is review, so switching to learn is a
    # re-entry: a concise <system-reminder>, NOT the full guide, under a fresh id (it belongs at the
    # current point, not back where the guide first landed).
    state = {
        "mode": "review",
        "messages": [HumanMessage(content="(full guide)", id=mode_guide_message_id("learn"))],
    }
    update = await engine.compile(state, ctx)
    assert update["mode"] == "learn"
    assert len(update["messages"]) == 1
    msg = update["messages"][0]
    assert msg.id != mode_guide_message_id("learn")
    assert msg.content.startswith("<system-reminder>") and "You are in **learn** mode" in msg.content
    assert "Tools permitted in **learn** mode" in msg.content


async def test_compile_default_idle_is_silent():
    engine = RootPromptEngine()
    # Fresh thread sitting in the default idle mode -> nothing to announce.
    assert await engine.compile({"messages": []}, RootAgentContext(app_state=AppContextStore())) is None


async def test_compile_switch_to_idle_announces_allowlist():
    engine = RootPromptEngine()
    ctx = RootAgentContext(app_state=AppContextStore(mode="idle"))
    # review -> idle: idle has no guide, so a bare <system> notice carrying the (changed) allowlist.
    update = await engine.compile({"mode": "review", "messages": []}, ctx)
    assert update["mode"] == "idle"
    assert len(update["messages"]) == 1
    msg = update["messages"][0]
    assert msg.content.startswith("<system>") and "idle" in msg.content
    assert "Tools permitted in **idle** mode" in msg.content
    assert msg.id != mode_guide_message_id("idle")          # fresh uuid; idle has no guide id


async def test_compile_mode_is_idempotent_after_commit():
    engine = RootPromptEngine()
    ctx = RootAgentContext(app_state=AppContextStore(mode="learn"))
    # Once committed, a compile whose state already sits at the desired mode says nothing further.
    assert await engine.compile({"mode": "learn", "messages": []}, ctx) is None


async def test_compile_no_mode_pass_without_app_state():
    engine = RootPromptEngine()
    # A session with no app-context store (e.g. a subagent) never touches mode.
    assert await engine.compile({"messages": []}, RootAgentContext()) is None


# ------------------------------------------------------------------------------------------------
# Message lifetime — identification (auto-tagger + inline marker); cleanup is stubbed
# ------------------------------------------------------------------------------------------------

def test_meta_centralizes_the_schema_with_defaults():
    m = HumanMessage(content="x")
    assert meta(m) == {}                       # untagged -> empty block, accessors fall back to defaults
    pin(m, "tail")
    set_lifetime(m, "semi-permanent")
    assert meta(m) == {"position": "pinned", "pin": "tail", "lifetime": "semi-permanent"}


def test_lifetime_defaults_to_permanent():
    m = HumanMessage(content="x")
    assert lifetime_of(m) == "permanent"
    assert lifetime_of(set_lifetime(m, "semi-permanent")) == "semi-permanent"


async def test_identify_autotags_whitelisted_bulky_tool_results():
    engine = RootPromptEngine(reclaim_tools=frozenset({"search"}), reclaim_threshold=5)
    state = {"messages": [
        ToolMessage(content="x" * 10, tool_call_id="a", name="search", id="big"),   # whitelisted + large
        ToolMessage(content="x" * 10, tool_call_id="b", name="other", id="off"),    # not whitelisted
        ToolMessage(content="xx", tool_call_id="c", name="search", id="small"),     # under threshold
    ]}
    update = await engine.compile(state, None)
    tagged = {m.id: m for m in update["messages"]}
    assert set(tagged) == {"big"}                # only the whitelisted, bulky result re-emitted
    assert lifetime_of(tagged["big"]) == "semi-permanent" and tagged["big"].tool_call_id == "a"


async def test_identify_skips_already_tagged_and_is_inert_by_default():
    # A self-tagged result is left alone — identification is idempotent across runs.
    pre = mark_reclaimable(ToolMessage(content="r", tool_call_id="a", name="search", id="m"))
    engine = RootPromptEngine(reclaim_tools=frozenset({"search"}), reclaim_threshold=0)
    assert await engine.compile({"messages": [pre]}, None) is None
    # With no policy configured, nothing auto-tags even a large result.
    big = ToolMessage(content="x" * 100, tool_call_id="a", name="search", id="m")
    assert await RootPromptEngine().compile({"messages": [big]}, None) is None


async def test_identification_is_a_base_engine_capability():
    engine = PromptEngine(reclaim_tools=frozenset({"dump"}), reclaim_threshold=1)
    update = await engine.compile(
        {"messages": [ToolMessage(content="lots", tool_call_id="a", name="dump", id="m")]}, None
    )
    assert lifetime_of(update["messages"][0]) == "semi-permanent"


async def test_cleanup_pass_drains_requests_and_stubs_the_group():
    # An explicit request drains from pending_cleanups; the engine's cleanup pass is the sole emitter.
    engine = RootPromptEngine()
    msg = mark_reclaimable(ToolMessage(content="big", tool_call_id="a", name="search", id="m"), group="search")
    update = await engine.compile({"messages": [msg], "pending_cleanups": [{"group": "search"}]}, None)
    stub = next(m for m in update["messages"] if m.id == "m")
    assert stub.tool_call_id == "a" and "reclaim" in stub.content    # stubbed in place, adjacency kept
    assert lifetime_of(stub) == "permanent"                          # promoted to a settled stub
    assert update["pending_cleanups"] is None                        # queue drained


def test_payload_message_tags_role():
    u = payload_message(MessagePayload(data="hi", role=MessagePayload.Role.USER))
    a = payload_message(MessagePayload(data="ok", role=MessagePayload.Role.AGENT))
    s = payload_message(MessagePayload(data="note", role=MessagePayload.Role.SYSTEM))
    assert (role_of(u), role_of(a), role_of(s)) == ("user", "agent", "system")


async def test_cleanup_pass_expires_old_semi_permanent_messages():
    engine = RootPromptEngine(expire_after=1)
    sp = mark_reclaimable(ToolMessage(content="big", tool_call_id="a", name="search", id="m"), group="search")
    user = set_role(HumanMessage(content="next", id="u"), "user")
    update = await engine.compile({"messages": [sp, user]}, None)
    stub = next(m for m in update["messages"] if m.id == "m")
    assert lifetime_of(stub) == "permanent" and "reclaim" in stub.content   # one user turn past expiry


async def test_freeze_inherited_promotes_semi_permanent_before_the_branch_marker():
    engine = RootPromptEngine()
    inherited = mark_reclaimable(
        ToolMessage(content="big", tool_call_id="a", name="search", id="m"), group="search"
    )
    ctx = RootAgentContext(topology=_topology(1, parent_id=0, parent_name="main"), node_id=1)
    update = await engine.compile({"messages": [inherited]}, ctx)
    frozen = next(m for m in update["messages"] if m.id == "m")
    assert lifetime_of(frozen) == "permanent" and frozen.content == inherited.content   # frozen, content kept


async def test_freeze_wins_over_expiry_for_inherited_messages():
    # An inherited semi-permanent message old enough to expire is frozen (kept), not stubbed — the freeze
    # runs after _cleanup, so its promotion is the last writer for that id (add_messages keeps last).
    engine = RootPromptEngine(expire_after=1)
    inherited = mark_reclaimable(
        ToolMessage(content="big", tool_call_id="a", name="search", id="m"), group="search"
    )
    user = set_role(HumanMessage(content="turn", id="u"), "user")
    ctx = RootAgentContext(topology=_topology(1, parent_id=0, parent_name="main"), node_id=1)
    update = await engine.compile({"messages": [inherited, user]}, ctx)
    resolved = [m for m in update["messages"] if m.id == "m"][-1]    # the last writer for this id wins
    assert lifetime_of(resolved) == "permanent" and resolved.content == inherited.content
