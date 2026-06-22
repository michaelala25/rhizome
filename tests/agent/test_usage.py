"""Token-usage accounting (``engine.usage`` + ``PromptEngine.report``): the two-layer split.

- *Ground truth* — ``provider_usage`` reads the latest model call's aggregate (and cache split) off the
  message history, with the ``response_metadata.usage`` fallback for integrations that skip
  ``input_token_details``.
- *Local attribution* — the engine estimates a per-message breakdown (plus synthetic system/tool slices)
  and normalizes it to the provider's ``input_tokens``, so the parts sum to the headline and the tool-schema
  cost the legacy breakdown missed is folded back in.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import tool

from rhizome.agent.engine import (
    compute_chat_model_max_tokens,
    PromptEngine,
    provider_usage,
    ProviderUsage,
    RootPromptEngine,
    UsageSegment,
)
from rhizome.agent.engine.metadata import set_role
from rhizome.agent.engine.resources import (
    global_resource_message_id,
    INDEX_RESOURCE_MESSAGE_ID,
    local_resource_message_id,
)
from rhizome.agent.engine.dump import format_report
from rhizome.agent.engine.root import branch_marker_message_id, mode_guide_message_id
from rhizome.agent.engine.usage import normalize, tool_kind


def _ai(input_tokens: int, output_tokens: int = 0, *, cache_read: int = 0, cache_creation: int = 0,
        content: str = "ok", **kw) -> AIMessage:
    return AIMessage(content=content, usage_metadata={
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {"cache_read": cache_read, "cache_creation": cache_creation},
    }, **kw)


# ------------------------------------------------------------------------------------------------
# Provider ground truth
# ------------------------------------------------------------------------------------------------

def test_provider_usage_reads_aggregate_and_cache_split():
    usage = provider_usage([HumanMessage(content="q"), _ai(1000, 50, cache_read=600, cache_creation=100)])
    assert usage == ProviderUsage(input_tokens=1000, output_tokens=50,
                                  cache_read_tokens=600, cache_creation_tokens=100)
    # cache reads + writes are SUBSETS of input_tokens, so fresh is the remainder.
    assert usage.fresh_input_tokens == 300
    assert usage.total_tokens == 1050


def test_provider_usage_falls_back_to_response_metadata():
    """Some integrations leave input_token_details empty — the cache split is recovered from the raw
    Anthropic usage keys on response_metadata."""
    msg = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 200, "output_tokens": 10, "total_tokens": 210},
        response_metadata={"usage": {"cache_read_input_tokens": 80, "cache_creation_input_tokens": 20}},
    )
    usage = provider_usage([msg])
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (80, 20)


def test_provider_usage_takes_the_latest_call():
    usage = provider_usage([_ai(100), HumanMessage(content="more"), _ai(900)])
    assert usage.input_tokens == 900


def test_provider_usage_none_before_first_response():
    assert provider_usage([HumanMessage(content="q")]) is None
    assert provider_usage([]) is None


# ------------------------------------------------------------------------------------------------
# Normalization
# ------------------------------------------------------------------------------------------------

def test_normalize_sums_exactly_to_target():
    segments = [UsageSegment("a", 33), UsageSegment("b", 33), UsageSegment("c", 34)]
    out = normalize(segments, 1000)
    assert sum(s.tokens for s in out) == 1000        # exact — no leftover drift from per-part rounding
    assert all(s.tokens >= 0 for s in out)
    assert [s.kind for s in out] == ["a", "b", "c"]  # order + identity preserved


def test_normalize_is_a_noop_without_a_target_or_content():
    segments = [UsageSegment("a", 10)]
    assert normalize(segments, None) is segments      # no ground truth yet → raw estimates
    assert normalize([UsageSegment("a", 0)], 500) == [UsageSegment("a", 0)]   # nothing to scale


# ------------------------------------------------------------------------------------------------
# Engine report
# ------------------------------------------------------------------------------------------------

def test_report_breakdown_sums_to_provider_total_and_includes_system_and_tools():
    @tool
    def lookup(query: str) -> str:
        """Look something up in the database with a fairly wordy description for schema bulk."""
        return ""

    engine = PromptEngine(system_prompt="You are a helpful agent. " * 30, tools=[lookup],
                          max_input_tokens=200_000)
    messages = [set_role(HumanMessage(content="hello there", id="u1"), "user"),
                _ai(1000, 40, cache_read=500)]
    report = engine.report({"messages": messages})

    # The breakdown agrees with the headline by construction, and the previously-invisible slices are there.
    assert sum(s.tokens for s in report.segments) == report.usage.input_tokens == 1000
    kinds = report.by_kind()
    assert kinds["system"] > 0 and kinds["tools"] > 0
    assert "user" in kinds and "agent" in kinds
    assert report.usage_percent == 100 * 1000 / 200_000


def test_report_skips_remove_messages():
    engine = PromptEngine()
    report = engine.report({"messages": [RemoveMessage(id="gone"), _ai(100)]})
    assert all(s.message_id != "gone" for s in report.segments)


def test_base_engine_distinguishes_tool_use_from_tool_result_and_agent():
    engine = PromptEngine()
    invocation = AIMessage(content="let me check", id="a1",
                           tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "tc1", "type": "tool_call"}])
    assert engine._message_kind(invocation) == "tool_use"
    assert engine._message_kind(ToolMessage(content="result", tool_call_id="tc1")) == "tool_result"
    assert engine._message_kind(AIMessage(content="just prose")) == "agent"
    assert engine._message_kind(set_role(HumanMessage(content="note"), "system")) == "system_notice"


def test_tool_kind_helper():
    assert tool_kind(AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1", "type": "tool_call"}])) == "use"
    assert tool_kind(ToolMessage(content="r", tool_call_id="1")) == "result"
    assert tool_kind(AIMessage(content="prose")) is None      # a plain AI turn is neither
    assert tool_kind(HumanMessage(content="hi")) is None


def test_report_before_first_response_is_raw_with_no_window_percentage():
    engine = PromptEngine(system_prompt="prompt", max_input_tokens=None)
    report = engine.report({"messages": [HumanMessage(content="hi")]})
    assert report.usage is None
    assert report.usage_percent is None
    assert report.segments                      # raw estimates still present for a pre-call display


# ------------------------------------------------------------------------------------------------
# Dump formatting (the shared engine.dump helpers)
# ------------------------------------------------------------------------------------------------

def test_format_report_renders_headline_kinds_and_ids():
    engine = PromptEngine(system_prompt="sys " * 10, max_input_tokens=100_000)
    report = engine.report({"messages": [
        set_role(HumanMessage(content="hello", id="u1"), "user"),
        _ai(1000, 20, cache_read=400),
    ]})
    text = format_report(report, node=2)
    assert "usage report — node=2" in text
    assert "cache_read=400" in text
    assert "(1.0%)" in text                      # 1000 / 100_000
    assert "by kind:" in text and "segments:" in text
    assert "id=u1" in text


# ------------------------------------------------------------------------------------------------
# Root engine classification
# ------------------------------------------------------------------------------------------------

def test_root_engine_classifies_its_own_message_ids():
    engine = RootPromptEngine()
    cases = {
        global_resource_message_id(7): "global_resource",
        local_resource_message_id(7): "local_resource",
        INDEX_RESOURCE_MESSAGE_ID: "resource_index",
        mode_guide_message_id("learn"): "guide",
        branch_marker_message_id(3): "branch_marker",
    }
    for message_id, expected in cases.items():
        assert engine._message_kind(HumanMessage(content="x", id=message_id)) == expected

    # Falls back to the generic classification for ordinary messages.
    assert engine._message_kind(set_role(HumanMessage(content="u"), "user")) == "user"
    assert engine._message_kind(set_role(HumanMessage(content="n"), "system")) == "system_notice"
    assert engine._message_kind(ToolMessage(content="r", tool_call_id="t")) == "tool_result"


# ------------------------------------------------------------------------------------------------
# Context window
# ------------------------------------------------------------------------------------------------

def test_compute_max_tokens_sums_input_and_output_window():
    model = SimpleNamespace(profile={"max_input_tokens": 200_000, "max_output_tokens": 64_000})
    assert compute_chat_model_max_tokens(model) == 264_000


def test_compute_max_tokens_is_none_when_profile_incomplete():
    assert compute_chat_model_max_tokens(SimpleNamespace(profile=None)) is None
    assert compute_chat_model_max_tokens(SimpleNamespace(profile={})) is None
    assert compute_chat_model_max_tokens(SimpleNamespace()) is None
