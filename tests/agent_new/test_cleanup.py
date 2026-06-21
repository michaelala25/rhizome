"""Cleanup: marking messages reclaimable, the request reducer, and ``apply_cleanup`` — the engine's sole
emitter (stub strategy today), with eligibility, message > request > default precedence, and dedup."""

from langchain_core.messages import HumanMessage, ToolMessage

from rhizome.agent_new.cleanup import (
    _effective_strategy,
    accumulate_cleanups,
    apply_cleanup,
    mark_reclaimable,
    promote,
    STUB_CONTENT,
)
from rhizome.agent_new.metadata import group_of, lifetime_of, set_role, set_strategy


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


def test_apply_cleanup_stubs_group_promotes_and_keeps_adjacency():
    stub = apply_cleanup([_semi("a", "search")], [{"group": "search"}])[0]
    assert stub.id == "a" and stub.tool_call_id == "a"          # identity + adjacency preserved
    assert stub.content == STUB_CONTENT and lifetime_of(stub) == "permanent"   # settled stub


def test_apply_cleanup_skips_wrong_group_and_ineligible():
    permanent = HumanMessage(content="keep", id="p")            # not semi-permanent -> ineligible
    edits = apply_cleanup([_semi("a", "search"), _semi("b", "files"), permanent], [{"group": "search"}])
    assert [e.id for e in edits] == ["a"]                       # only the matching-group semi-perm message


def test_apply_cleanup_dedupes_across_requests():
    edits = apply_cleanup([_semi("a", "search")], [{"group": "search"}, {"group": "search"}])
    assert [e.id for e in edits] == ["a"]                       # reclaimed once despite two requests


def test_apply_cleanup_unimplemented_strategy_falls_back_to_stub():
    tagged = set_strategy(_semi("a", "search"), "summarize")    # only stub is built; others fall back
    stub = apply_cleanup([tagged], [{"group": "search"}])[0]
    assert stub.content == STUB_CONTENT and lifetime_of(stub) == "permanent"


# ----- promote (branch freeze) ----- #

def test_promote_freezes_lifetime_preserving_content():
    m = mark_reclaimable(ToolMessage(content="x", tool_call_id="a", name="g", id="m"), group="g")
    frozen = promote(m)
    assert frozen.id == "m" and frozen.content == m.content     # byte-identical -> cache spine preserved
    assert lifetime_of(frozen) == "permanent" and group_of(frozen) == "g"
    assert promote(frozen) is frozen                            # no-op once permanent


# ----- apply_cleanup: age expiry ----- #

def test_apply_cleanup_expires_after_user_turns_counting_only_user_role():
    sp = _semi("a", "g")
    user = set_role(HumanMessage(content="next", id="u1"), "user")
    system = set_role(HumanMessage(content="<system>x</system>", id="s1"), "system")
    # One genuine user turn after the semi-perm message; the system message does not count.
    assert [e.id for e in apply_cleanup([sp, system, user], expire_after=1)] == ["a"]
    assert apply_cleanup([sp, system, user], expire_after=2) == []      # needs 2 user turns, only 1
    assert apply_cleanup([sp, system], expire_after=1) == []            # no user turns after -> kept
