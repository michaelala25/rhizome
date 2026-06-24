"""Cleanup: marking messages reclaimable, the request reducer, and ``apply_cleanup`` — the engine's sole
emitter (stub strategy today), with eligibility, message > request > default precedence, and dedup."""

from langchain_core.messages import HumanMessage, ToolMessage

from rhizome.agent.base import accumulate_cleanups
from rhizome.agent.engine.cleanup import (
    _effective_strategy,
    apply_cleanup,
    apply_hydrations,
    mark_reclaim_ineligible,
    mark_reclaimable,
    promote,
    reclamation_status,
    STUB_CONTENT,
)
from rhizome.agent.engine.metadata import (
    expire_after_of,
    group_of,
    hydrations_of,
    is_reclaim_ineligible,
    lifetime_of,
    set_expire_after,
    set_hydrations,
    set_role,
    set_strategy,
)


# ----- marking ----- #

def test_mark_reclaimable_tags_group_marks_and_is_idempotent():
    original = ToolMessage(content="big result", tool_call_id="tc-1", name="search", id="m1")
    marked = mark_reclaimable(original, group="search")
    # A re-emittable copy: same identity (so add_messages replaces in place), tagged + grouped + marked.
    assert marked is not original and marked.id == "m1" and marked.tool_call_id == "tc-1"
    assert lifetime_of(marked) == "semi-permanent" and group_of(marked) == "search"
    assert marked.content.startswith("big result") and marked.content.endswith("[reclaimable · search]")
    # The original still living in state is untouched.
    assert original.content == "big result" and lifetime_of(original) == "permanent"
    # Idempotent: re-marking a semi-permanent message returns it unchanged.
    assert mark_reclaimable(marked, group="search") is marked


def test_mark_reclaimable_without_group_uses_a_bare_marker():
    marked = mark_reclaimable(ToolMessage(content="x", tool_call_id="a", id="m"))
    assert marked.content.endswith("[reclaimable]") and group_of(marked) is None


def test_mark_reclaimable_can_bake_a_per_message_expire_after():
    marked = mark_reclaimable(ToolMessage(content="x", tool_call_id="a", id="m"), "g", expire_after=3)
    assert marked.additional_kwargs["rhizome"]["expire_after"] == 3   # rides as a per-message override


def test_mark_reclaim_ineligible_is_offwire_and_idempotent():
    original = ToolMessage(content="data", tool_call_id="a", name="t", id="m")
    stamped = mark_reclaim_ineligible(original)
    # Off-wire: a same-id copy with content untouched (no marker) — the prompt cache never sees the change.
    assert stamped is not original and stamped.id == "m" and stamped.content == "data"
    assert is_reclaim_ineligible(stamped) and not is_reclaim_ineligible(original)
    assert lifetime_of(stamped) == "permanent"                       # eligibility, not lifetime
    assert mark_reclaim_ineligible(stamped) is stamped               # idempotent


# ----- request reducer ----- #

def test_accumulate_cleanups_appends_and_drains():
    a, b = {"group": "g1"}, {"group": "g2"}
    assert accumulate_cleanups(None, [a]) == [a]        # first write
    assert accumulate_cleanups([a], [b]) == [a, b]      # parallel sources compose
    assert accumulate_cleanups([a, b], None) == []      # None drains


# ----- strategy precedence ----- #

def test_effective_strategy_message_over_request_over_default():
    plain = ToolMessage(content="x", tool_call_id="a", id="m")
    assert _effective_strategy(plain, {"group": "g"}, "stub") == "stub"                       # engine default
    assert _effective_strategy(plain, {"group": "g", "strategy": "summarize"}, "stub") == "summarize"  # request
    tagged = set_strategy(ToolMessage(content="x", tool_call_id="a", id="m"), "stub+store")
    assert _effective_strategy(tagged, {"group": "g", "strategy": "summarize"}, "stub") == "stub+store"  # message


# ----- apply_cleanup (stub) ----- #

def _semi(id_: str, group: str, content: str = "data") -> ToolMessage:
    return mark_reclaimable(ToolMessage(content=content, tool_call_id=id_, name=group, id=id_), group=group)


async def test_apply_cleanup_stubs_group_promotes_and_keeps_adjacency():
    stub = (await apply_cleanup([_semi("a", "search")], [{"group": "search"}]))[0]
    assert stub.id == "a" and stub.tool_call_id == "a"          # identity + adjacency preserved
    assert stub.content == STUB_CONTENT and lifetime_of(stub) == "permanent"   # settled stub


async def test_apply_cleanup_skips_wrong_group_and_ineligible():
    permanent = HumanMessage(content="keep", id="p")            # not semi-permanent -> ineligible
    edits = await apply_cleanup([_semi("a", "search"), _semi("b", "files"), permanent], [{"group": "search"}])
    assert [e.id for e in edits] == ["a"]                       # only the matching-group semi-perm message


async def test_apply_cleanup_dedupes_across_requests():
    edits = await apply_cleanup([_semi("a", "search")], [{"group": "search"}, {"group": "search"}])
    assert [e.id for e in edits] == ["a"]                       # reclaimed once despite two requests


async def test_apply_cleanup_summarize_without_a_summarizer_falls_back_to_stub():
    tagged = set_strategy(_semi("a", "search"), "summarize")    # summarize strategy, but no summarizer wired
    stub = (await apply_cleanup([tagged], [{"group": "search"}]))[0]
    assert stub.content == STUB_CONTENT and lifetime_of(stub) == "permanent"


async def test_apply_cleanup_summarizes_when_a_summarizer_is_provided():
    tagged = set_strategy(_semi("a", "search"), "summarize")

    async def summarize(targets):                               # the engine injects this (a subagent batch)
        return {m.id: f"summary of {m.id}" for m in targets}

    out = (await apply_cleanup([tagged], [{"group": "search"}], summarize=summarize))[0]
    assert out.id == "a" and lifetime_of(out) == "permanent" and is_reclaim_ineligible(out)
    assert "summary of a" in out.content and out.content != STUB_CONTENT   # condensed, not emptied


# ----- promote (branch freeze) ----- #

def test_promote_freezes_lifetime_preserving_content():
    m = mark_reclaimable(ToolMessage(content="x", tool_call_id="a", name="g", id="m"), group="g")
    frozen = promote(m)
    assert frozen.id == "m" and frozen.content == m.content     # byte-identical -> cache spine preserved
    assert lifetime_of(frozen) == "permanent" and group_of(frozen) == "g"
    assert is_reclaim_ineligible(frozen)                        # settled -> auto-tagger won't re-tag it
    assert promote(frozen) is frozen                            # no-op once permanent


# ----- apply_cleanup: age expiry ----- #

async def test_apply_cleanup_expires_after_user_turns_counting_only_user_role():
    sp = _semi("a", "g")
    user = set_role(HumanMessage(content="next", id="u1"), "user")
    system = set_role(HumanMessage(content="<system>x</system>", id="s1"), "system")
    # One genuine user turn after the semi-perm message; the system message does not count.
    assert [e.id for e in await apply_cleanup([sp, system, user], expire_after=1)] == ["a"]
    assert await apply_cleanup([sp, system, user], expire_after=2) == []   # needs 2 user turns, only 1
    assert await apply_cleanup([sp, system], expire_after=1) == []         # no user turns after -> kept


async def test_apply_cleanup_per_message_expire_after_overrides_the_engine_default():
    sp = set_expire_after(_semi("a", "g"), 2)                           # this message's own age is 2 turns
    u1 = set_role(HumanMessage(content="u", id="u1"), "user")
    u2 = set_role(HumanMessage(content="u", id="u2"), "user")
    assert await apply_cleanup([sp, u1], expire_after=1) == []          # 1 turn < its own 2 -> kept (own wins)
    assert [e.id for e in await apply_cleanup([sp, u1, u2], expire_after=1)] == ["a"]   # 2 >= 2 -> reclaimed


async def test_apply_cleanup_per_message_expire_after_fires_with_engine_default_off():
    sp = set_expire_after(_semi("a", "g"), 1)
    u1 = set_role(HumanMessage(content="u", id="u1"), "user")
    assert [e.id for e in await apply_cleanup([sp, u1], expire_after=None)] == ["a"]   # own age fires, default off


# ----- apply_hydrations (keep longer) ----- #

def test_apply_hydrations_bumps_expiry_and_counts_for_the_group():
    [out] = apply_hydrations([_semi("a", "search")], [{"group": "search"}],
                             default_expiry=5, bump=5, max_hydrations=3)
    assert out.id == "a" and lifetime_of(out) == "semi-permanent"
    assert expire_after_of(out) == 10 and hydrations_of(out) == 1    # default 5 + bump 5, first hydration
    # A second hydration bumps from the message's own tag (10 -> 15) and counts again.
    [out2] = apply_hydrations([out], [{"group": "search"}], default_expiry=5, bump=5, max_hydrations=3)
    assert expire_after_of(out2) == 15 and hydrations_of(out2) == 2


def test_apply_hydrations_promotes_after_max():
    sp = set_hydrations(_semi("a", "search"), 2)                     # already hydrated twice
    [out] = apply_hydrations([sp], [{"group": "search"}], default_expiry=5, bump=5, max_hydrations=3)
    assert lifetime_of(out) == "permanent" and is_reclaim_ineligible(out)   # the 3rd keep settles it


def test_apply_hydrations_touches_only_the_requested_group_semi_perm():
    keep = HumanMessage(content="x", id="p")                         # not semi-permanent
    out = apply_hydrations([_semi("a", "search"), _semi("b", "files"), keep],
                           [{"group": "search"}], default_expiry=5, bump=5, max_hydrations=3)
    assert [e.id for e in out] == ["a"]


# ----- reclamation_status (the agent's view) ----- #

def test_reclamation_status_summarizes_per_group_with_min_turns():
    a = _semi("a", "search")                                         # default-expiry path
    b = set_expire_after(_semi("b", "search"), 2)                    # its own expiry of 2 turns
    user = set_role(HumanMessage(content="u", id="u"), "user")       # one user turn after both
    # search: 2 messages; min turns-left = min(5-1, 2-1) = 1 (the soonest cleanup)
    assert reclamation_status([a, b, user], default_expiry=5) == {"search": (2, 1)}
    assert reclamation_status([], default_expiry=5) == {}
