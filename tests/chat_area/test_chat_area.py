"""ChatAreaModel: message/payload wiring, runs through the stream router, interrupts, branching.

Same strategy as the graph tests — real stack over scripted models. ``StreamingEchoModel`` actually
streams chunks so the router's messages-mode path is exercised; plain ``EchoModel`` suffices where
only state/feed outcomes matter.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.types import interrupt

from rhizome.agent_new.state import RootAgentState
from rhizome.app.chat_area.branch import BranchPointModel
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.agent_new.engine import MessagePayload, StateUpdatePayload
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.app.chat_pane.messages.static import ChatMessageModel
from rhizome.app.chat_pane.messages.tool import ToolMessageModel
from rhizome.app.chat_pane.thinking import ThinkingIndicatorModel
from rhizome.tui.types import Mode, Role

from tests.agent_new.fakes import ai_contents, build_runtime, EchoModel, ToolOnceModel


class StreamingEchoModel(EchoModel):
    """EchoModel that actually streams — chunks flow through langgraph's messages mode into the
    router's ``on_message`` path."""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        full = self._respond(messages).content
        step = max(1, len(full) // 3)
        for i in range(0, len(full), step):
            yield ChatGenerationChunk(message=AIMessageChunk(content=full[i: i + step]))


@tool
def choices_tool() -> str:
    """Asks the user to pick an option."""
    answer = interrupt({"type": "choices", "message": "Pick one:", "options": ["Apple", "Banana"]})
    return f"picked:{answer}"


def make_area(model_factory=None, tools=()) -> ChatAreaModel:
    runtime = build_runtime(
        model_factory or (lambda: StreamingEchoModel()), tools=tools, state_schema=RootAgentState
    )
    return ChatAreaModel(runtime)


async def wait_idle(node, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while node.busy:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("node never went idle")
        await asyncio.sleep(0.01)


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


def entries(node) -> list:
    return [item.entry for item in node.feed]


class Listener:
    """Strongly-held subscriber recording the area's view-facing events."""

    def __init__(self, area: ChatAreaModel) -> None:
        self.busy, self.hints, self.interrupt_edges, self.cursor_moves = [], [], [], []
        area.subscribe(area.Callbacks.OnBusyChanged, self.on_busy)
        area.subscribe(area.Callbacks.OnHint, self.on_hint)
        area.subscribe(area.Callbacks.OnInterruptChanged, self.on_interrupt)
        area.subscribe(area.Callbacks.OnCursorMoved, self.on_cursor)

    def on_busy(self, node, busy):
        self.busy.append((node, busy))

    def on_hint(self, msg):
        self.hints.append(msg)

    def on_interrupt(self, node):
        self.interrupt_edges.append((node, node.pending_interrupt is not None))

    def on_cursor(self, cursor):
        self.cursor_moves.append(cursor)


# ------------------------------------------------------------------------------------------------
# Messages & payloads
# ------------------------------------------------------------------------------------------------

async def test_append_message_dedupes_and_forwards_by_role():
    area = make_area()
    node = area.cursor.node

    assert area.append_message("note", Role.SYSTEM) is not None
    assert area.append_message("note", Role.SYSTEM) is None          # consecutive identical SYSTEM
    area.append_message("boom", Role.ERROR)                          # UI-side noise: never forwarded
    area.append_message("hello", Role.USER)

    assert [e.content for e in entries(node)] == ["note", "boom", "hello"]
    queued = node.queued
    assert [(p.role, p.data) for p in queued if isinstance(p, MessagePayload)] == [
        (MessagePayload.Role.SYSTEM, "note"),
        (MessagePayload.Role.USER, "hello"),
    ]


async def test_mode_writes_store_and_verbosity_payload_reaches_branch_state():
    area = make_area()
    node = area.cursor.node

    area.set_mode(Mode.LEARN)
    area.set_verbosity("detailed")

    # Mode is the live SSOT store now — set_mode writes it directly, NOT the payload queue. Verbosity
    # still travels as a StateUpdatePayload (its store migration is future work), so only it is queued.
    assert node.app_state.mode == "learn"
    assert sum(isinstance(p, StateUpdatePayload) for p in node.queued) == 1
    assert [e.content for e in entries(node)] == ["Entered learn mode."]   # UI-only, not forwarded

    area.append_message("hi", Role.USER)
    area.submit()
    await wait_idle(node)
    assert (await node.agent_state)["verbosity"] == "detailed"


@pytest.mark.xfail(
    reason="mode→display now flows store -> (prompt-engine store/state alignment) -> state -> router. "
           "That alignment is pending, and this harness runs the base PromptEngine which has none, so "
           "state['mode'] stays unset and the router tags IDLE. Re-enable once the engine commits "
           "AppContextStore.mode into RootAgentState.",
    strict=False,
)
async def test_mode_set_while_idle_tags_that_same_runs_segments():
    """The lag killer: a mode set while idle is live in the store before the run, so once the prompt
    engine commits it into state at the first compile, the RunStateView folds it before chunks arrive
    and the router tags this run's segments with it."""
    area = make_area()
    node = area.cursor.node

    area.set_mode(Mode.LEARN, silent=True)
    assert node.app_state.mode == "learn"          # the store carries it immediately (the part done here)

    area.append_message("hi", Role.USER)
    area.submit()
    await wait_idle(node)

    agent_msg = next(e for e in entries(node) if isinstance(e, AgentMessageModel))
    assert agent_msg.mode == Mode.LEARN            # needs the pending engine alignment (xfail until then)


# ------------------------------------------------------------------------------------------------
# Runs
# ------------------------------------------------------------------------------------------------

async def test_submit_streams_into_the_feed():
    area = make_area()
    listener = Listener(area)
    node = area.cursor.node

    area.append_message("hello", Role.USER)
    area.submit()
    assert area.agent_busy()
    await wait_idle(node)

    kinds = [type(e) for e in entries(node)]
    assert kinds == [ChatMessageModel, AgentMessageModel]            # thinking indicator cleaned up
    agent_msg = entries(node)[1]
    assert agent_msg.body.startswith("echo:hello") and not agent_msg.streaming
    assert listener.busy == [(node, True), (node, False)]


async def test_tool_calls_route_into_tool_lists():
    @tool
    def noop_tool() -> str:
        """Does nothing."""
        return "done"

    area = make_area(lambda: ToolOnceModel(tool_name="noop_tool"), tools=(noop_tool,))
    node = area.cursor.node

    area.append_message("go", Role.USER)
    area.submit()
    await wait_idle(node)

    tool_lists = [e for e in entries(node) if isinstance(e, ToolMessageModel)]
    assert len(tool_lists) == 1 and tool_lists[0].tools == [("noop_tool", {})]
    assert not any(isinstance(e, ThinkingIndicatorModel) for e in entries(node))


async def test_submit_soft_fails_on_busy_and_frozen():
    area = make_area(lambda: StreamingEchoModel(delay=0.3))
    listener = Listener(area)
    node = area.cursor.node

    area.append_message("first", Role.USER)
    area.submit()
    area.submit()                                                    # busy: hint, no second run
    assert len(listener.hints) == 1 and "already responding" in listener.hints[0]
    await wait_idle(node)

    await area.branch()                                              # freezes the root
    area.submit(cursor=area.conversation_graph.root_cursor())
    assert len(listener.hints) == 2 and "frozen" in listener.hints[1]


async def test_cancel_midstream_leaves_the_cancelled_trace():
    area = make_area(lambda: StreamingEchoModel(delay=5.0))
    node = area.cursor.node

    area.append_message("slow", Role.USER)
    area.submit()
    await asyncio.sleep(0.1)                                         # let the run reach the model
    area.cancel()
    await wait_idle(node)

    texts = [e.content for e in entries(node) if isinstance(e, ChatMessageModel)]
    assert texts == ["slow", "(user cancelled)"]
    assert not any(isinstance(e, (ThinkingIndicatorModel, AgentMessageModel)) for e in entries(node))


# ------------------------------------------------------------------------------------------------
# Interrupts
# ------------------------------------------------------------------------------------------------

async def test_present_interrupt_resolve_dismiss_and_task_cancel():
    area = make_area()
    listener = Listener(area)
    node = area.cursor.node

    # Resolve: the awaited value comes back, the pending slot opens and closes.
    vm = UserChoicesModel.from_interrupt({"message": "?", "options": ["a", "b"]})
    task = asyncio.create_task(area.present_interrupt(vm))
    await wait_for(lambda: node.pending_interrupt is vm)
    vm.resolve("a")
    assert await task == "a" and node.pending_interrupt is None

    # Dismissal (vm.cancel): resolves to None — the run would continue.
    vm2 = UserChoicesModel.from_interrupt({"message": "?", "options": ["a"]})
    task2 = asyncio.create_task(area.present_interrupt(vm2))
    await wait_for(lambda: node.pending_interrupt is vm2)
    vm2.cancel()
    assert await task2 is None and node.pending_interrupt is None

    # Task cancellation: propagates (the session's cancel path must run), slot still cleared.
    vm3 = UserChoicesModel.from_interrupt({"message": "?", "options": ["a"]})
    task3 = asyncio.create_task(area.present_interrupt(vm3))
    await wait_for(lambda: node.pending_interrupt is vm3)
    task3.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task3
    assert node.pending_interrupt is None

    # Both edges fired each time: blocked, unblocked.
    assert [edge for _, edge in listener.interrupt_edges] == [True, False] * 3


async def test_interrupt_through_a_real_stream_resumes_with_the_choice():
    area = make_area(lambda: ToolOnceModel(tool_name="choices_tool"), tools=(choices_tool,))
    node = area.cursor.node

    area.append_message("go", Role.USER)
    area.submit()
    await wait_for(lambda: node.pending_interrupt is not None)

    vm = node.pending_interrupt
    assert isinstance(vm, UserChoicesModel) and vm in entries(node)
    vm.resolve("Apple")
    await wait_idle(node)

    # The resume value flowed through the tool into the thread (display chunking is covered by
    # test_submit_streams — ToolOnceModel doesn't stream).
    assert ai_contents(await node.agent_state)[-1] == "after-tool:picked:Apple"
    tool_lists = [e for e in entries(node) if isinstance(e, ToolMessageModel)]
    assert tool_lists and tool_lists[0].tools == [("choices_tool", {})]
    assert node.pending_interrupt is None


# ------------------------------------------------------------------------------------------------
# Branching
# ------------------------------------------------------------------------------------------------

async def test_branch_makes_continuation_plus_branch_and_checks_out_the_branch():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    area.append_message("context", Role.SYSTEM)

    new = await area.branch(name="alt")

    # Two children: the continuation (leftmost, inherits "main") and the named branch.
    continuation, alt = graph.children(root)
    assert root.frozen and (continuation.name, alt.name) == ("main", "alt")
    assert new.node is alt and area.cursor.node is alt

    # The indicator landed in the root's feed before the freeze, and tracks the descent.
    indicator = next(e for e in entries(root) if isinstance(e, BranchPointModel))
    assert indicator.selected_child is alt

    # A later fork at the (now frozen) root adds exactly one sibling — no second continuation.
    await area.branch(cursor=graph.root_cursor())
    assert len(graph.children(root)) == 3


async def test_branch_hoists_out_of_an_empty_live_leaf():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    area.append_message("context", Role.SYSTEM)

    await area.branch(name="alt")        # cursor now on the empty, live "alt" leaf
    await area.branch(name="alt2")       # hoists: sibling at the root, not a child of "alt"

    names = [c.name for c in graph.children(root)]
    assert names == ["main", "alt", "alt2"]
    assert graph.children(graph.node(area.cursor)) == ()
    assert area.cursor.nodes() == (root, area.cursor.node)


async def test_branch_with_prompt_streams_on_the_new_branch_only():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    area.append_message("hello", Role.USER)

    new = await area.branch(prompt="hi there")
    await wait_idle(new.node)

    agent_msgs = [e for e in entries(new.node) if isinstance(e, AgentMessageModel)]
    assert agent_msgs and agent_msgs[-1].body.startswith("echo:hi there")
    assert not any(isinstance(e, AgentMessageModel) for e in entries(root))


async def test_swap_sibling_updates_indicator_and_navigation_soft_fails_hint():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    area.append_message("context", Role.SYSTEM)
    await area.branch(name="alt")

    indicator = next(e for e in entries(root) if isinstance(e, BranchPointModel))
    continuation, alt = graph.children(root)

    area.swap_sibling(-1)
    assert area.cursor.node is continuation and indicator.selected_child is continuation

    listener = Listener(area)
    area.swap_sibling(-1)                # leftmost already: hint, cursor unchanged
    assert listener.hints and area.cursor.node is continuation
    assert listener.cursor_moves == []
