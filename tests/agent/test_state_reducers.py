"""Tests for ``RhizomeAgentState`` reducer wiring and dispatch-time
parameter-name collisions.

Three things are guarded:

1. ``merge_typeddict_field`` behaves as advertised: ``None``-clears,
   ``None``-initializes, shallow-merges disjoint keys, LWWs same-key
   conflicts.
2. The reducer is actually attached via ``Annotated`` to every nullable
   ``TypedDict`` field on ``RhizomeAgentState`` — catches an accidental
   removal during a future state.py edit.
3. No tool built by any ``build_*_tools`` factory has a parameter whose
   name collides with ``StructuredTool._arun``'s keyword-only slots
   (``config``, ``run_manager``). LangChain silently absorbs such kwargs
   into its own dispatch signature, leaving the tool body to receive
   ``None``. The reserved set is discovered via ``inspect`` rather than
   hard-coded so it tracks LangChain upstream changes.
"""

from __future__ import annotations

import inspect
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest
from langchain_core.tools.structured import StructuredTool

from rhizome.agent.state import (
    RhizomeAgentState,
    merge_typeddict_field,
)
from rhizome.agent.tools.app import build_app_tools
from rhizome.agent.tools.core import build_core_tools
from rhizome.agent.tools.flashcard_proposal import build_flashcard_proposal_tools
from rhizome.agent.tools.guide import build_guide_tools
from rhizome.agent.tools.resources import build_resource_tools
from rhizome.agent.tools.review import build_review_tools
from rhizome.agent.tools.sql import build_sql_tools


# ---------------------------------------------------------------------------
# merge_typeddict_field behavior
# ---------------------------------------------------------------------------

class TestMergeTypedDictField:
    def test_right_none_clears(self):
        assert merge_typeddict_field({"a": 1, "b": 2}, None) is None

    def test_left_none_initializes(self):
        assert merge_typeddict_field(None, {"a": 1}) == {"a": 1}

    def test_disjoint_keys_compose(self):
        # The core parallel-safety promise.
        assert merge_typeddict_field(
            {"a": 1, "b": 2}, {"c": 3}
        ) == {"a": 1, "b": 2, "c": 3}

    def test_same_key_lww(self):
        # Right wins on conflict; the original value is replaced wholesale.
        assert merge_typeddict_field(
            {"a": 1, "b": 2}, {"b": 99}
        ) == {"a": 1, "b": 99}

    def test_both_none(self):
        assert merge_typeddict_field(None, None) is None


# ---------------------------------------------------------------------------
# Reducer wiring on RhizomeAgentState
# ---------------------------------------------------------------------------

# Fields that should be reduced via merge_typeddict_field. Anything else on
# the state (mode, messages) is intentionally LWW.
_REDUCED_FIELDS = ("review", "flashcard_proposal_state", "commit_proposal_state")


@pytest.mark.parametrize("field_name", _REDUCED_FIELDS)
def test_state_field_uses_merge_reducer(field_name):
    hints = get_type_hints(RhizomeAgentState, include_extras=True)
    annotation = hints[field_name]
    assert get_origin(annotation) is Annotated, (
        f"{field_name} must be Annotated[..., merge_typeddict_field] for "
        f"parallel tool calls to compose; got {annotation!r}"
    )
    metadata = get_args(annotation)[1:]
    assert merge_typeddict_field in metadata, (
        f"{field_name} is missing merge_typeddict_field in its Annotated "
        f"metadata; reducer attached: {metadata!r}"
    )


# ---------------------------------------------------------------------------
# Dispatch-time parameter-name collisions
# ---------------------------------------------------------------------------

def _reserved_tool_param_names() -> set[str]:
    """Discover parameter names that LangChain's ``StructuredTool`` dispatch
    will silently absorb. These are the keyword-only parameters of ``_run``
    and ``_arun`` — anything sitting between ``*args`` and ``**kwargs`` in
    those signatures captures a same-named kwarg before it reaches the user
    function.
    """
    reserved: set[str] = set()
    for fname in ("_run", "_arun"):
        sig = inspect.signature(getattr(StructuredTool, fname))
        for name, param in sig.parameters.items():
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                reserved.add(name)
    return reserved


def _all_built_tools() -> dict[str, object]:
    """Instantiate every tool-builder closure with stub dependencies and
    flatten the resulting dicts. We only need the tool objects' signatures,
    so the stubs never get called.
    """
    stub = lambda: None  # noqa: E731
    builders = [
        build_app_tools(stub),
        build_core_tools(stub),
        build_flashcard_proposal_tools(stub),
        build_guide_tools(),
        build_resource_tools(stub),
        build_review_tools(stub),
        build_sql_tools(stub),
    ]
    out: dict[str, object] = {}
    for d in builders:
        out.update(d)
    return out


def test_no_tool_param_collides_with_structured_tool_dispatch():
    reserved = _reserved_tool_param_names()
    # Sanity: at minimum we know about config/run_manager today.
    assert {"config", "run_manager"} <= reserved, (
        f"Expected langchain to reserve at least config/run_manager; "
        f"discovered {reserved!r}"
    )

    offenders: list[tuple[str, str]] = []
    for name, tool in _all_built_tools().items():
        coro = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
        if coro is None:
            continue
        params = set(inspect.signature(coro).parameters)
        for clash in params & reserved:
            offenders.append((name, clash))

    assert not offenders, (
        "Tool parameter names collide with StructuredTool._arun's "
        "keyword-only slots, so LangChain will silently absorb the value "
        "and the tool body will receive None. Rename the offending "
        f"parameter(s):\n{offenders!r}"
    )
