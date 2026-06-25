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

from rhizome.agent.state import RootAgentState
from rhizome.app.browser.browser import BrowserModel
from rhizome.app.chat_area.branch import BranchPointModel
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.options import Options, OptionScope
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.agent.engine import MessagePayload, UsageReport
from rhizome.app.chat_area.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.shell import ShellCommandModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.app.chat_area.messages.tool import ToolMessageModel
from rhizome.app.chat_area.thinking import ThinkingIndicatorModel
from rhizome.app.chat_area.welcome_message import WelcomeMessageModel
from rhizome.tui.types import Mode, Role

from tests.agent.fakes import ai_contents, build_runtime, EchoModel, ToolOnceModel


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


def make_area(model_factory=None, tools=(), debug=False) -> ChatAreaModel:
    runtime = build_runtime(
        model_factory or (lambda: StreamingEchoModel()), tools=tools, state_schema=RootAgentState
    )
    return ChatAreaModel(runtime, debug=debug)


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


async def test_mode_and_verbosity_write_the_app_state_store():
    area = make_area()
    node = area.cursor.node

    area.set_mode(Mode.LEARN)
    area.set_verbosity("verbose")

    # Mode and verbosity are both live SSOT settings on the node's LocalAppContextStore now — the setters
    # write the store directly, never the payload queue. (Verbosity has no prompt-engine consumer yet,
    # so it does not reach agent state; mode's commit into state is the engine's job at compile time.)
    assert node.app_state.mode == "learn"
    assert node.app_state.verbosity == "verbose"
    assert not node.queued                                                 # both are store writes
    assert [e.content for e in entries(node)] == ["Entered learn mode."]   # mode posts a UI-only line


async def test_welcome_banner_seeds_the_root_feed_only_when_requested():
    assert entries(make_area().cursor.node) == []                          # off by default

    area = ChatAreaModel(
        build_runtime(lambda: StreamingEchoModel(), state_schema=RootAgentState),
        options=Options(OptionScope.Session),
        show_welcome=True,
    )
    welcomes = [e for e in entries(area.cursor.node) if isinstance(e, WelcomeMessageModel)]
    assert len(welcomes) == 1


# ------------------------------------------------------------------------------------------------
# Feed visibility (display-only filters)
# ------------------------------------------------------------------------------------------------

async def test_show_thinking_option_gates_is_visible_and_emits_a_toggle_tick():
    options = Options(OptionScope.Session)
    area = ChatAreaModel(build_runtime(lambda: EchoModel(), state_schema=RootAgentState), options=options)

    thinking = AgentMessageModel(thinking=True)
    answer = AgentMessageModel(thinking=False)

    # Shown by default — both an answer and a thinking segment are visible.
    assert area.is_visible(thinking) is True
    assert area.is_visible(answer) is True

    ticks: list[bool] = []
    def on_vis() -> None:                                        # local var keeps the weak-held sub alive
        ticks.append(True)
    area.subscribe(area.Callbacks.OnVisibilityChanged, on_vis)

    options.set(Options.ShowThinking, "disabled")
    assert area.is_visible(thinking) is False                   # thinking hidden...
    assert area.is_visible(answer) is True                      # ...answers untouched
    assert len(ticks) == 1                                      # one view-facing reconcile tick

    options.set(Options.ShowThinking, "enabled")
    assert area.is_visible(thinking) is True
    assert len(ticks) == 2


async def test_is_visible_defaults_to_shown_without_an_option_service():
    area = make_area()                                          # no options in scope
    assert area.is_visible(AgentMessageModel(thinking=True)) is True


# ------------------------------------------------------------------------------------------------
# Slash commands
# ------------------------------------------------------------------------------------------------

async def test_rename_branch_command_renames_current_leaf():
    area = make_area()
    await area.commands.execute("/rename-branch My Branch")     # RAW parser keeps the space + casing
    assert area.cursor.node.name == "My Branch"


async def test_clear_command_hints_and_leaves_feed_intact():
    area = make_area()
    listener = Listener(area)
    node = area.cursor.node
    area.append_message("keep me", Role.USER, to_agent=False)

    await area.commands.execute("/clear")

    assert [e.content for e in entries(node) if isinstance(e, ChatMessageModel)] == ["keep me"]
    assert listener.hints                                       # /clear is a no-op hint for now


async def test_options_command_appends_editor_with_options_else_hints():
    bare = make_area()                                          # no options in scope
    bare_listener = Listener(bare)
    await bare.commands.execute("/options")
    assert bare_listener.hints
    assert entries(bare.cursor.node) == []

    area = ChatAreaModel(
        build_runtime(lambda: StreamingEchoModel(), state_schema=RootAgentState),
        options=Options(OptionScope.Session),
    )
    await area.commands.execute("/options")
    assert isinstance(entries(area.cursor.node)[-1], OptionsEditorModel)


async def test_browse_command_appends_browser_with_factory_else_hints():
    bare = make_area()                                          # no session factory in scope
    bare_listener = Listener(bare)
    await bare.commands.execute("/browse")
    assert bare_listener.hints
    assert entries(bare.cursor.node) == []

    area = ChatAreaModel(
        build_runtime(lambda: StreamingEchoModel(), state_schema=RootAgentState),
        session_factory=object(),                               # Browser ctor doesn't touch the DB
    )
    await area.commands.execute("/browse")
    assert isinstance(entries(area.cursor.node)[-1], BrowserModel)


def test_real_commands_always_registered_demo_commands_gated_on_debug():
    real = {"idle", "learn", "review", "branch", "rename-branch", "browse", "options", "commit", "clear", "echo"}
    demo = {"test-interrupt", "test-choices", "test-warning-choices", "test-multiple-choices",
            "test-sql-confirmation", "test-flashcards", "test-commit-proposal", "test-flashcard-proposal"}

    # Demo /test-* commands register only under the app's --debug flag.
    names = {name for name, _ in make_area().commands.rows()}
    assert real <= names
    assert not (demo & names)

    debug_names = {name for name, _ in make_area(debug=True).commands.rows()}
    assert real <= debug_names
    assert demo <= debug_names


@pytest.mark.xfail(
    reason="mode→display now flows store -> (prompt-engine store/state alignment) -> state -> router. "
           "That alignment is pending, and this harness runs the base PromptEngine which has none, so "
           "state['mode'] stays unset and the router tags IDLE. Re-enable once the engine commits "
           "LocalAppContextStore.mode into RootAgentState.",
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


async def test_usage_report_caches_on_the_node_and_drives_the_visible_status_bar():
    area = make_area()
    node = area.cursor.node

    area.append_message("hello", Role.USER)
    area.submit()
    await wait_idle(node)

    report = node.usage_report
    assert report is not None                              # the router cached it on the node
    assert area.status_bar.usage_report is report          # the visible branch drives the shared bar
    assert any(s.kind == "user" for s in report.segments)  # the breakdown covers the conversation


async def test_branch_inherits_usage_report_and_cursor_change_tracks_the_leaf():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root

    area.append_message("hello", Role.USER)
    area.submit()
    await wait_idle(root)
    root_report = root.usage_report
    assert root_report is not None

    alt = (await area.branch(name="alt")).node
    assert alt.usage_report is root_report                 # derive carried it across the branch
    assert area.status_bar.usage_report is root_report     # cursor moved to alt → bar re-synced

    # Diverge alt's cache, then navigate root <-> alt: the bar tracks whichever leaf is checked out.
    sentinel = UsageReport(usage=None, max_input_tokens=None, segments=())
    alt.usage_report = sentinel

    area.set_cursor(graph.root_cursor())
    assert area.status_bar.usage_report is root_report
    area.set_cursor(alt)
    assert area.status_bar.usage_report is sentinel


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


# ------------------------------------------------------------------------------------------------
# Input dispatch
# ------------------------------------------------------------------------------------------------
#
# Drive the bridge-imported ChatInputModel directly (set_buffer + submit) to exercise the routing
# that ChatAreaModel hangs off the input's OnSubmitted.

async def test_plain_chat_submission_appends_user_and_starts_a_run():
    area = make_area()
    node = area.cursor.node

    area.chat_input.set_buffer("hello")
    area.chat_input.submit()

    assert area.agent_busy()                        # a run kicked off
    assert area.chat_input.buffer == ""             # accepted: buffer cleared, history pushed
    assert area.chat_input.can_history_prev()
    await wait_idle(node)

    user_msgs = [e.content for e in entries(node) if isinstance(e, ChatMessageModel) and e.role == Role.USER]
    assert user_msgs == ["hello"]
    agent_msgs = [e for e in entries(node) if isinstance(e, AgentMessageModel)]
    assert agent_msgs and agent_msgs[-1].body.startswith("echo:hello")


async def test_empty_submission_does_nothing():
    area = make_area()
    area.chat_input.set_buffer("   ")
    area.chat_input.submit()                        # CHAT state: the input VM no-ops an empty submit

    assert not area.agent_busy()
    assert entries(area.cursor.node) == []


async def test_shell_submission_appends_a_shell_command_and_runs_it():
    area = make_area()
    node = area.cursor.node

    area.chat_input.set_buffer("!echo hello")
    area.chat_input.submit()

    assert area.chat_input.buffer == ""             # accepted (side-channel command)
    assert not area.agent_busy()                    # no agent run — shell is side-channel

    shells = [e for e in entries(node) if isinstance(e, ShellCommandModel)]
    assert len(shells) == 1 and shells[0].command == "echo hello"

    # The executor was scheduled on the worker (asyncio.create_task in tests); let it finish.
    await wait_for(lambda: shells[0].finished_at is not None)
    assert shells[0].returncode == 0 and "hello" in shells[0].joined_output


async def test_slash_command_dispatches_through_the_registry():
    area = make_area()
    node = area.cursor.node

    area.chat_input.set_buffer("/help")          # /help is built into the registry core
    area.chat_input.submit()

    assert area.chat_input.buffer == ""          # accepted; dispatch is scheduled
    await wait_for(lambda: any(
        isinstance(e, ChatMessageModel) and e.role == Role.SYSTEM and "commands" in e.content.lower()
        for e in entries(node)
    ))


async def test_unknown_slash_command_surfaces_an_error():
    area = make_area()
    node = area.cursor.node

    area.chat_input.set_buffer("/nope")
    area.chat_input.submit()

    await wait_for(lambda: any(
        isinstance(e, ChatMessageModel) and e.role == Role.ERROR and "nope" in e.content for e in entries(node)
    ))


async def test_mode_slash_command_runs():
    area = make_area()
    node = area.cursor.node

    area.chat_input.set_buffer("/learn")
    area.chat_input.submit()

    await wait_for(lambda: node.app_state.mode == "learn")


async def test_branch_prompt_submission_forks_and_streams_on_the_new_branch():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    area.append_message("seed", Role.USER)

    area.chat_input.set_buffer("/branch tell me more")
    area.chat_input.submit()
    assert area.chat_input.buffer == ""             # accepted; branch() runs on the scheduler

    # branch() is async (scheduled, not awaited here) — wait for the streamed prompt to land on a
    # child branch rather than the root.
    await wait_for(lambda: any(
        isinstance(e, AgentMessageModel) for child in graph.children(root) for e in entries(child)
    ))
    branch_node = next(
        c for c in graph.children(root) if any(isinstance(e, AgentMessageModel) for e in entries(c))
    )
    await wait_idle(branch_node)

    agent_msgs = [e for e in entries(branch_node) if isinstance(e, AgentMessageModel)]
    assert agent_msgs[-1].body.startswith("echo:tell me more")
    assert not any(isinstance(e, AgentMessageModel) for e in entries(root))


async def test_plain_chat_blocked_while_busy_keeps_the_buffer():
    area = make_area(lambda: StreamingEchoModel(delay=0.3))
    listener = Listener(area)
    node = area.cursor.node

    area.chat_input.set_buffer("first")
    area.chat_input.submit()
    assert area.agent_busy()

    area.chat_input.set_buffer("second")
    area.chat_input.submit()                        # busy: hint, buffer kept for retry, no append

    assert listener.hints and "already responding" in listener.hints[-1]
    assert area.chat_input.buffer == "second"
    await wait_idle(node)

    user_msgs = [e.content for e in entries(node) if isinstance(e, ChatMessageModel) and e.role == Role.USER]
    assert user_msgs == ["first"]                   # "second" never reached the feed
