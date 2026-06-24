"""Tests for ``ask_user_input`` — the interrupt-driven multiple-choice tool.

``interrupt()`` only resolves inside a running graph, so these patch it with a stub that records the payload
the tool emits and returns a canned resume value. That isolates the tool's own contract: the interrupt
payload shape (which ``stream_router`` keys on to build the widget) and how the resume value is formatted
back into the tool-result string.
"""

from types import SimpleNamespace

import pytest

from rhizome.agent.app_context import AppContextStore
from rhizome.agent.tools import TOOL_VISIBILITY, ToolVisibility, build_app_tools
from rhizome.agent.tools import app as app_module


@pytest.fixture
def ask_tool():
    return build_app_tools()["ask_user_input"]


@pytest.fixture
def set_mode_tool():
    return build_app_tools()["set_mode"]


@pytest.fixture
def cleanup_context_tool():
    return build_app_tools()["cleanup_context"]


@pytest.fixture
def hydrate_tool():
    return build_app_tools()["hydrate"]


def _runtime_with(app_state) -> SimpleNamespace:
    """A minimal stand-in for the ToolRuntime the tool reads ``ctx.app_state`` off of."""
    return SimpleNamespace(context=SimpleNamespace(app_state=app_state))


@pytest.fixture
def interrupt_stub(monkeypatch):
    """Replace the module-level ``interrupt`` with a stub: capture its payload, return a preset value."""
    box: dict = {}

    def fake_interrupt(payload):
        box["payload"] = payload
        return box["resume"]

    monkeypatch.setattr(app_module, "interrupt", fake_interrupt)
    return box


async def test_single_question_emits_choices_interrupt(ask_tool, interrupt_stub):
    interrupt_stub["resume"] = "Red"
    out = await ask_tool.ainvoke({"questions": [
        {"name": "Color", "prompt": "Pick a color", "options": ["Red", "Blue"]},
    ]})

    assert interrupt_stub["payload"] == {
        "type": "choices", "message": "Pick a color", "options": ["Red", "Blue"],
    }
    assert out == "User selected: Red"


async def test_multiple_questions_emit_multiple_choice_interrupt(ask_tool, interrupt_stub):
    interrupt_stub["resume"] = {"Color": "Red", "Size": "Large"}
    out = await ask_tool.ainvoke({"questions": [
        {"name": "Color", "prompt": "Pick a color", "options": ["Red", "Blue"]},
        {"name": "Size", "prompt": "Pick a size", "options": ["Small", "Large"]},
    ]})

    payload = interrupt_stub["payload"]
    assert payload["type"] == "multiple_choice"
    assert payload["questions"] == [
        {"name": "Color", "prompt": "Pick a color", "options": ["Red", "Blue"]},
        {"name": "Size", "prompt": "Pick a size", "options": ["Small", "Large"]},
    ]
    assert out == "User selections:\nColor: Red\nSize: Large"


async def test_set_mode_writes_the_store(set_mode_tool):
    """The tool writes the live store (the SSOT) — it does NOT return a state-update Command."""
    store = AppContextStore(mode="idle")
    out = await set_mode_tool.coroutine(mode="learn", runtime=_runtime_with(store))
    assert store.mode == "learn"
    assert out == "Mode is now: learn."


async def test_set_mode_rejects_invalid_mode(set_mode_tool):
    store = AppContextStore(mode="idle")
    out = await set_mode_tool.coroutine(mode="bogus", runtime=_runtime_with(store))
    assert store.mode == "idle"          # store left untouched
    assert "Invalid mode" in out


async def test_set_mode_graceful_when_store_unwired(set_mode_tool):
    """Dormant-safe until ``RootAgentContext.app_state`` is wired: no app_state → a readable message,
    not an AttributeError."""
    runtime = SimpleNamespace(context=SimpleNamespace())   # no app_state attribute
    out = await set_mode_tool.coroutine(mode="learn", runtime=runtime)
    assert "unavailable" in out.lower()


async def test_cleanup_context_files_a_request_and_acknowledges(cleanup_context_tool):
    """Files a declarative CleanupRequest onto ``pending_cleanups`` (the engine is the sole emitter of the
    actual edits) plus this call's own tool_result, in one Command update."""
    runtime = SimpleNamespace(tool_call_id="tc-1")
    cmd = await cleanup_context_tool.coroutine(group="search", runtime=runtime)
    assert cmd.update["pending_cleanups"] == [{"group": "search"}]
    msg = cmd.update["messages"][0]
    assert msg.tool_call_id == "tc-1" and "search" in msg.content


async def test_hydrate_files_a_request_and_acknowledges(hydrate_tool):
    """The keep-it-longer mirror: files a HydrateRequest onto ``pending_hydrations`` plus its tool_result."""
    runtime = SimpleNamespace(tool_call_id="tc-2")
    cmd = await hydrate_tool.coroutine(group="query", runtime=runtime)
    assert cmd.update["pending_hydrations"] == [{"group": "query"}]
    msg = cmd.update["messages"][0]
    assert msg.tool_call_id == "tc-2" and "query" in msg.content


def test_app_tools_register_low_visibility():
    """Guards the decorator order: registered under the *tool* names (not the function names) and at
    LOW — the level the old app.py silently failed to apply."""
    build_app_tools()
    assert TOOL_VISIBILITY["ask_user_input"] is ToolVisibility.LOW
    assert TOOL_VISIBILITY["set_mode"] is ToolVisibility.LOW
    assert TOOL_VISIBILITY["cleanup_context"] is ToolVisibility.LOW
    assert TOOL_VISIBILITY["hydrate"] is ToolVisibility.LOW
