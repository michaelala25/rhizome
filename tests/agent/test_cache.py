"""Anthropic prompt-cache breakpoints: the mechanic (``apply_cache_control``), the priority/budget
allocator (``allocate``), and the root engine's ``prepare`` placement — gates, positions, and the
view-only invariant (breakpoints annotate COPIES; the checkpointed state messages are never touched).
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from rhizome.agent.context import RootAgentContext
from rhizome.agent.engine.cache import (
    allocate,
    apply_cache_control,
    Breakpoint,
    cache_control,
    cache_control_of,
    MAX_BREAKPOINTS,
)
from rhizome.agent.engine.dump import format_request
from rhizome.agent.engine.metadata import lifetime_of, pin, pin_of, set_lifetime
from rhizome.agent.engine.resources import (
    global_resource_message_id,
    INDEX_RESOURCE_MESSAGE_ID,
    local_resource_message_id,
)
from rhizome.agent.engine.root import branch_marker_message_id, RootPromptEngine


# ------------------------------------------------------------------------------------------------
# Test doubles
# ------------------------------------------------------------------------------------------------

class Ref:
    """Minimal ``OptionRef`` stand-in: ``get()`` returns a fixed value."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


class Req:
    """Minimal ``ModelRequest`` stand-in: prepare reads ``.messages``/``.system_message`` and calls
    ``.override``."""

    def __init__(self, messages, system_message=None) -> None:
        self.messages = messages
        self.system_message = system_message

    def override(self, *, messages) -> "Req":
        return Req(messages, self.system_message)


def cached_engine(*, ttl: str = "5m", toggle: str = "enabled", supported: bool = True) -> RootPromptEngine:
    return RootPromptEngine(cache_supported=supported, prompt_cache=Ref(toggle), prompt_cache_ttl=Ref(ttl))


def ttls(messages) -> dict:
    """Map of message id -> the breakpoint TTL on it, for the messages that carry a breakpoint."""
    return {m.id: cc["ttl"] for m in messages if (cc := cache_control_of(m))}


@pytest.fixture(autouse=True)
def _no_dump(monkeypatch):
    """The dumps fire unconditionally in ``prepare`` (a bring-up aid); keep these tests off the filesystem.
    ``format_request`` is exercised directly in its own test below."""
    monkeypatch.setattr("rhizome.agent.engine.root.dump_request", lambda *a, **k: None)
    monkeypatch.setattr("rhizome.agent.engine.root.dump_report", lambda *a, **k: None)


# ------------------------------------------------------------------------------------------------
# The mechanic: apply_cache_control / cache_control descriptors
# ------------------------------------------------------------------------------------------------

def test_cache_control_maps_the_ttl_option():
    assert cache_control("5m") == {"type": "ephemeral", "ttl": "5m"}
    assert cache_control("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_apply_cache_control_wraps_string_content_on_a_copy():
    original = pin(HumanMessage(content="hello", id="x"), "head")
    out = apply_cache_control(original, cache_control("5m"))

    assert out is not original and original.content == "hello"          # copy; original untouched
    assert out.content == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]
    assert out.id == "x" and pin_of(out) == "head"                      # id + metadata ride along


def test_apply_cache_control_annotates_only_the_last_block_of_a_list():
    original = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], id="y")
    out = apply_cache_control(original, cache_control("1h"))

    assert out.content[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in out.content[0]
    assert original.content[-1] == {"type": "text", "text": "b"}        # original list block untouched


def test_apply_cache_control_is_none_when_unannotatable():
    # Empty content of either shape has no block to mark — the allocator treats this as "did not place".
    assert apply_cache_control(AIMessage(content=""), cache_control("5m")) is None
    assert apply_cache_control(AIMessage(content=[]), cache_control("5m")) is None


def test_apply_cache_control_preserves_lifetime_metadata():
    msg = set_lifetime(HumanMessage(content="r", id="m"), "semi-permanent")
    out = apply_cache_control(msg, cache_control("5m"))
    assert lifetime_of(out) == "semi-permanent" and cache_control_of(out)["ttl"] == "5m"


# ------------------------------------------------------------------------------------------------
# The allocator: priority order IS list order, budget IS the integer
# ------------------------------------------------------------------------------------------------

def _targets(indices):
    """A breakpoint per index that resolves to ``messages[i]`` (late-binding avoided via a default arg)."""
    return [Breakpoint(str(i), lambda ms, i=i: ms[i], cache_control("5m")) for i in indices]


def test_allocate_drops_the_lowest_priority_over_budget():
    msgs = [HumanMessage(content=str(i), id=str(i)) for i in range(5)]
    # Five DISTINCT candidates, budget 4 -> the last (lowest priority) is dropped.
    out = allocate(msgs, _targets([0, 1, 2, 3, 4]), budget=4)
    assert [i for i, m in enumerate(out) if cache_control_of(m)] == [0, 1, 2, 3]


def test_allocate_dedupes_and_frees_the_slot_for_a_lower_candidate():
    msgs = [HumanMessage(content=str(i), id=str(i)) for i in range(5)]
    # a->4, b->2, c->2 (collision, skipped), d->0, e->1: the collision doesn't spend budget, so e fits.
    out = allocate(msgs, _targets([4, 2, 2, 0, 1]), budget=4)
    assert [i for i, m in enumerate(out) if cache_control_of(m)] == [0, 1, 2, 4]


def test_allocate_each_candidate_stamps_its_own_ttl():
    msgs = [HumanMessage(content=str(i), id=str(i)) for i in range(2)]
    candidates = [
        Breakpoint("a", lambda ms: ms[0], cache_control("1h")),
        Breakpoint("b", lambda ms: ms[1], cache_control("5m")),
    ]
    out = allocate(msgs, candidates)
    assert ttls(out) == {"0": "1h", "1": "5m"}


def test_allocate_skips_non_applicable_and_preserves_identity_when_empty():
    msgs = [HumanMessage(content="a", id="a")]
    same = allocate(msgs, [Breakpoint("none", lambda ms: None, cache_control("5m"))])
    assert same is msgs                                                 # nothing placed -> same object


def test_allocate_never_exceeds_max_breakpoints():
    msgs = [HumanMessage(content=str(i), id=str(i)) for i in range(6)]
    out = allocate(msgs, _targets(range(6)))                            # default budget = MAX_BREAKPOINTS
    assert sum(1 for m in out if cache_control_of(m)) == MAX_BREAKPOINTS == 4


# ------------------------------------------------------------------------------------------------
# Root prepare: gates (provider + toggle)
# ------------------------------------------------------------------------------------------------

async def test_prepare_places_no_breakpoints_when_provider_unsupported():
    engine = cached_engine(supported=False)            # e.g. an OpenAI build — can't take breakpoints
    out = await engine.prepare(Req([HumanMessage(content="u", id="u"), AIMessage(content="a", id="a")]), None)
    assert ttls(out.messages) == {}


async def test_prepare_places_no_breakpoints_when_toggle_disabled():
    engine = cached_engine(toggle="disabled")
    out = await engine.prepare(Req([HumanMessage(content="u", id="u"), AIMessage(content="a", id="a")]), None)
    assert ttls(out.messages) == {}


async def test_prepare_default_engine_never_caches():
    # The default engine (no cache config) is what every other test constructs; it must stay inert.
    out = await RootPromptEngine().prepare(Req([HumanMessage(content="u", id="u")]), None)
    assert ttls(out.messages) == {}


# ------------------------------------------------------------------------------------------------
# Root prepare: positions
# ------------------------------------------------------------------------------------------------

async def test_prepare_before_tail_is_the_floor():
    # No resources, no marker: a single breakpoint on the last message, excluding nothing but a tail.
    engine = cached_engine()
    out = await engine.prepare(
        Req([HumanMessage(content="u", id="u"), AIMessage(content="a", id="a")]), None
    )
    assert ttls(out.messages) == {"a": "5m"}


async def test_prepare_before_tail_excludes_the_floated_tail():
    engine = cached_engine()
    idx = pin(HumanMessage(content="IDX", id=INDEX_RESOURCE_MESSAGE_ID), "tail")
    out = await engine.prepare(Req([HumanMessage(content="u", id="u"), idx]), None)
    # The tail-pinned reminder floats to the end and stays UNcached; the breakpoint sits before it.
    assert ttls(out.messages) == {"u": "5m"}


async def test_prepare_skips_head_without_global_resources():
    engine = cached_engine()
    loc = pin(HumanMessage(content="L", id=local_resource_message_id(2)), "branch")
    out = await engine.prepare(Req([loc, HumanMessage(content="leaf", id="leaf")]), RootAgentContext(node_id=0))
    # Root node (no marker) + no global block -> only the before_tail floor fires.
    assert ttls(out.messages) == {"leaf": "5m"}


async def test_prepare_places_head_branch_leaf_and_tail():
    engine = cached_engine(ttl="1h")
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    marker = HumanMessage(content="<system>branched</system>", id=branch_marker_message_id(5))
    loc = pin(HumanMessage(content="L", id=local_resource_message_id(2)), "branch")
    idx = pin(HumanMessage(content="IDX", id=INDEX_RESOURCE_MESSAGE_ID), "tail")
    request = Req([
        SystemMessage(content="sys"), g,
        HumanMessage(content="inherited", id="inh"), marker, loc,
        HumanMessage(content="leaf", id="leaf"), idx,
    ])
    out = await engine.prepare(request, RootAgentContext(node_id=5))

    # head -> G ; branch_leaf -> the message BEFORE the marker ; before_tail -> leaf.
    assert ttls(out.messages) == {global_resource_message_id(1): "1h", "inh": "1h", "leaf": "1h"}
    # And crucially NOT on the node-specific marker, the local block, or the floated tail.
    assert cache_control_of(next(m for m in out.messages if m.id == branch_marker_message_id(5))) is None
    assert cache_control_of(next(m for m in out.messages if m.id == local_resource_message_id(2))) is None
    assert cache_control_of(next(m for m in out.messages if m.id == INDEX_RESOURCE_MESSAGE_ID)) is None


async def test_prepare_branch_leaf_is_none_when_marker_leads_the_body():
    # Nothing precedes the marker -> no branch_leaf boundary (the head/before_tail still apply).
    engine = cached_engine()
    marker = HumanMessage(content="<system>branched</system>", id=branch_marker_message_id(7))
    out = await engine.prepare(
        Req([marker, HumanMessage(content="leaf", id="leaf")]), RootAgentContext(node_id=7)
    )
    assert ttls(out.messages) == {"leaf": "5m"}                         # only the floor
    assert cache_control_of(out.messages[0]) is None                   # not on the marker


async def test_prepare_honors_the_live_ttl():
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    body = [g, HumanMessage(content="leaf", id="leaf")]
    assert set(ttls((await cached_engine(ttl="5m").prepare(Req(list(body)), None)).messages).values()) == {"5m"}
    assert set(ttls((await cached_engine(ttl="1h").prepare(Req(list(body)), None)).messages).values()) == {"1h"}


async def test_prepare_dynamic_ttl_splits_the_floor_from_the_prefix():
    engine = cached_engine(ttl="dynamic")
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    marker = HumanMessage(content="<system>branched</system>", id=branch_marker_message_id(5))
    request = Req([g, HumanMessage(content="inh", id="inh"), marker, HumanMessage(content="leaf", id="leaf")])
    out = await engine.prepare(request, RootAgentContext(node_id=5))
    # Stable prefix anchors (head, branch_leaf) get 1h; the volatile before_tail floor gets 5m.
    assert ttls(out.messages) == {global_resource_message_id(1): "1h", "inh": "1h", "leaf": "5m"}


# ------------------------------------------------------------------------------------------------
# Root prepare: the view-only invariant
# ------------------------------------------------------------------------------------------------

async def test_prepare_never_mutates_the_state_messages():
    engine = cached_engine()
    g = pin(HumanMessage(content="G", id=global_resource_message_id(1)), "head")
    leaf = HumanMessage(content="leaf", id="leaf")
    await engine.prepare(Req([g, leaf]), RootAgentContext(node_id=0))
    # The originals still carry plain string content and no breakpoint — only the wire copies were marked.
    assert g.content == "G" and leaf.content == "leaf"
    assert cache_control_of(g) is None and cache_control_of(leaf) is None


# ------------------------------------------------------------------------------------------------
# Debug dump surfaces the breakpoint
# ------------------------------------------------------------------------------------------------

def test_format_request_shows_the_cache_tag():
    cached = apply_cache_control(HumanMessage(content="prefix", id="p"), cache_control("1h"))
    plain = HumanMessage(content="tail", id="t")
    text = format_request(Req([cached, plain], system_message=SystemMessage(content="sys")))
    assert "cache=1h" in text and "cache=-" in text
