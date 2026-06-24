"""Commit mode (Phase 1): the chat-area VM's global selection state + lifecycle, and the pure
cross-branch payload assembly handed to the agent. View-side selection/decoration is exercised
separately; here the focus is the VM contract — what gets staged, what gets queued, what events fire.
"""

import asyncio

from rhizome.agent.engine import MessagePayload, StateUpdatePayload
from rhizome.agent.state import RootAgentState
from rhizome.app.chat_area import commit
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.options import Options, OptionScope
from rhizome.app.chat_area.chat_input import ChatInputModel
from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.tui.types import Mode, Role

from tests.agent.fakes import build_runtime, EchoModel


def make_area() -> ChatAreaModel:
    return ChatAreaModel(build_runtime(lambda: EchoModel(), state_schema=RootAgentState))


async def wait_idle(node, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while node.busy:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("node never went idle")
        await asyncio.sleep(0.01)


def agent_msg(body: str, *, mode=Mode.IDLE, thinking=False, cancelled=False, streaming=False):
    """A *finished* agent segment by default — the shape commit selection accepts."""
    vm = AgentMessageModel(mode=mode, thinking=thinking)
    vm.body = body
    vm.streaming = streaming
    vm.cancelled = cancelled
    return vm


class CommitListener:
    """Strongly-held subscriber recording commit-mode edges, selection toggles, and hints."""

    def __init__(self, area: ChatAreaModel) -> None:
        self.mode_edges, self.selection_edges, self.hints = [], [], []
        area.subscribe(area.Callbacks.OnCommitModeChanged, self._mode)
        area.subscribe(area.Callbacks.OnCommitSelectionChanged, self._selection)
        area.subscribe(area.Callbacks.OnHint, self._hint)

    def _mode(self, active):
        self.mode_edges.append(active)

    def _selection(self, node, item, staged):
        self.selection_edges.append((item.id, staged))

    def _hint(self, msg):
        self.hints.append(msg)


# ------------------------------------------------------------------------------------------------
# Eligibility policy (commit.is_selectable) — pure
# ------------------------------------------------------------------------------------------------

def test_is_selectable_gates_thinking_unfinished_and_by_level():
    # Finished agent answers are eligible; thinking / streaming / cancelled segments never are.
    assert commit.is_selectable(agent_msg("ans"))
    assert not commit.is_selectable(agent_msg("ans", thinking=True))
    assert not commit.is_selectable(agent_msg("ans", streaming=True))
    assert not commit.is_selectable(agent_msg("ans", cancelled=True))

    # Under the default "all_agent" level, user/system chat messages are not eligible.
    assert not commit.is_selectable(ChatMessageModel(Role.USER, "hi"))
    # "all" admits the user's own messages but never system lines.
    assert commit.is_selectable(ChatMessageModel(Role.USER, "hi"), level="all")
    assert not commit.is_selectable(ChatMessageModel(Role.SYSTEM, "x"), level="all")

    # "learn_only" gates agent answers to learn mode.
    assert commit.is_selectable(agent_msg("a", mode=Mode.LEARN), level="learn_only")
    assert not commit.is_selectable(agent_msg("a", mode=Mode.IDLE), level="learn_only")


def test_is_commit_selectable_reads_the_commit_selectable_option():
    """With an option service in scope, selectability follows ``CommitSelectable`` (default
    ``learn_only``) — both the view's policy *and* the toggle guard. Regression: the level was hardcoded
    to ``all_agent``, so a non-learn agent message was wrongly stageable under ``learn_only``."""
    area = ChatAreaModel(
        build_runtime(lambda: EchoModel(), state_schema=RootAgentState),
        options=Options(OptionScope.Session),                    # CommitSelectable defaults to learn_only
    )
    node = area.cursor.node
    idle_item = area.append_item(agent_msg("idle answer", mode=Mode.IDLE))
    learn_item = area.append_item(agent_msg("learn answer", mode=Mode.LEARN))

    assert not area.is_commit_selectable(idle_item.entry)        # non-learn agent message: not eligible
    assert area.is_commit_selectable(learn_item.entry)

    area.enter_commit_mode()
    area.toggle_commit_selection(node, idle_item)                # the guard refuses it too
    assert area.commit_selection_count == 0
    area.toggle_commit_selection(node, learn_item)
    assert area.commit_selection_count == 1


# ------------------------------------------------------------------------------------------------
# Payload assembly (commit.build_payload) — the cross-branch core
# ------------------------------------------------------------------------------------------------

async def test_build_payload_spans_branches_in_graph_order():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root

    # Root branch: a user prompt then an agent answer.
    area.append_message("root Q", Role.USER, to_agent=False)
    root_ans = area.append_item(agent_msg("root A", mode=Mode.LEARN))

    # Fork; the new "alt" branch gets its own answer.
    await area.branch(name="alt")
    alt = area.cursor.node
    alt_ans = area.append_item(agent_msg("alt A"))

    area.enter_commit_mode()
    area.toggle_commit_selection(alt, alt_ans)        # check off out of order...
    area.toggle_commit_selection(root, root_ans)      # ...the root answer second

    payload = commit.build_payload(graph, area._commit_selection)

    # Graph order (root before alt), not selection order; branch provenance + within-branch user_context.
    assert payload == [
        {"content": "root A", "role": "agent", "branch": "main", "user_context": "root Q"},
        {"content": "alt A", "role": "agent", "branch": "alt"},
    ]


async def test_build_payload_includes_user_messages_only_at_the_all_level():
    # build_payload reflects whatever is in the selection; eligibility was already enforced at toggle.
    area = make_area()
    node = area.cursor.node
    user_item = area.append_item(ChatMessageModel(Role.USER, "a user note"))
    agent_item = area.append_item(agent_msg("an answer"))

    area.enter_commit_mode()
    # Default level rejects the user message; force it into the selection to prove assembly handles it.
    area._commit_selection[user_item.id] = (node, user_item)
    area.toggle_commit_selection(node, agent_item)

    payload = commit.build_payload(area.conversation_graph, area._commit_selection)
    assert [(e["content"], e["role"]) for e in payload] == [("a user note", "user"), ("an answer", "agent")]


# ------------------------------------------------------------------------------------------------
# Lifecycle: enter / exit
# ------------------------------------------------------------------------------------------------

def test_enter_and_exit_drive_input_state_and_events():
    area = make_area()
    listener = CommitListener(area)

    area.enter_commit_mode()
    assert area.commit_active
    assert area.chat_input.state == ChatInputModel.State.COMMIT
    assert listener.mode_edges == [True]

    area.enter_commit_mode()                      # idempotent — no duplicate event
    assert listener.mode_edges == [True]

    area.exit_commit_mode()
    assert not area.commit_active
    assert area.chat_input.state == ChatInputModel.State.CHAT
    assert listener.mode_edges == [True, False]


def test_enter_refused_while_an_interrupt_is_pending():
    area = make_area()
    listener = CommitListener(area)
    area.cursor.node.pending_interrupt = object()     # simulate a blocked branch

    area.enter_commit_mode()

    assert not area.commit_active
    assert listener.mode_edges == []
    assert any("pending prompt" in h for h in listener.hints)


def test_exit_clears_selection_by_default_but_can_retain():
    area = make_area()
    node = area.cursor.node
    item = area.append_item(agent_msg("a"))

    area.enter_commit_mode()
    area.toggle_commit_selection(node, item)
    area.exit_commit_mode(clear=False)
    assert area.commit_selection_count == 1           # retained across a peek out of the mode

    area.enter_commit_mode()
    area.exit_commit_mode()                            # default clears
    assert area.commit_selection_count == 0


# ------------------------------------------------------------------------------------------------
# Selection toggles
# ------------------------------------------------------------------------------------------------

def test_toggle_stages_and_unstages_and_emits():
    area = make_area()
    node = area.cursor.node
    item = area.append_item(agent_msg("ans"))
    listener = CommitListener(area)
    area.enter_commit_mode()

    area.toggle_commit_selection(node, item)
    assert area.is_committed(item.id) and area.commit_selection_count == 1
    area.toggle_commit_selection(node, item)
    assert not area.is_committed(item.id) and area.commit_selection_count == 0
    assert listener.selection_edges == [(item.id, True), (item.id, False)]


def test_toggle_ignores_ineligible_entries_and_is_noop_outside_mode():
    area = make_area()
    node = area.cursor.node
    sys_item = area.append_item(ChatMessageModel(Role.SYSTEM, "x"))     # never eligible
    agent_item = area.append_item(agent_msg("a"))

    area.toggle_commit_selection(node, agent_item)                      # not in commit mode
    assert area.commit_selection_count == 0

    area.enter_commit_mode()
    area.toggle_commit_selection(node, sys_item)                        # eligible? no
    assert area.commit_selection_count == 0


def test_feed_removal_prunes_a_staged_item():
    area = make_area()
    node = area.cursor.node
    item = area.append_item(agent_msg("a"))
    area.enter_commit_mode()
    area.toggle_commit_selection(node, item)
    assert area.is_committed(item.id)

    area.remove_item(item)
    assert not area.is_committed(item.id)


# ------------------------------------------------------------------------------------------------
# Submit: hand-off + soft-fails
# ------------------------------------------------------------------------------------------------

async def test_submit_commit_queues_payload_plus_turn_and_leaves_the_mode():
    area = make_area()
    node = area.cursor.node
    item = area.append_item(agent_msg("an answer", mode=Mode.LEARN))
    area.enter_commit_mode()
    area.toggle_commit_selection(node, item)

    area.submit_commit("make it concise")

    # Inspect the backlog synchronously — the run task is scheduled but has not drained it yet.
    updates = [p for p in node.queued if isinstance(p, StateUpdatePayload)]
    assert len(updates) == 1
    cps = updates[0].data["commit_proposal_state"]
    assert cps["proposal"] == [] and cps["proposal_diff"] is None
    assert [e["content"] for e in cps["payload"]] == ["an answer"]
    turns = [p for p in node.queued if isinstance(p, MessagePayload) and p.role == MessagePayload.Role.USER]
    assert [p.data for p in turns] == ["make it concise"]

    # Mode left, selection handed off, a run kicked off on this branch.
    assert not area.commit_active and area.commit_selection_count == 0
    assert area.agent_busy()
    await wait_idle(node)


async def test_submit_commit_default_instruction_when_input_empty():
    area = make_area()
    node = area.cursor.node
    area.append_item(agent_msg("one"))
    item = area.append_item(agent_msg("two"))
    area.enter_commit_mode()
    area.toggle_commit_selection(node, item)

    area.submit_commit("")        # empty instructions → a generated request turn

    turns = [p for p in node.queued if isinstance(p, MessagePayload) and p.role == MessagePayload.Role.USER]
    assert turns and turns[0].data == "Commit the 1 selected message into knowledge entries."
    await wait_idle(node)


async def test_submit_commit_soft_fails_on_empty_selection_and_frozen_leaf():
    area = make_area()
    graph = area.conversation_graph
    root = graph.root
    item = area.append_item(agent_msg("a"))
    listener = CommitListener(area)

    # Empty selection: hint, stay in the mode.
    area.enter_commit_mode()
    area.submit_commit()
    assert area.commit_active and any("no messages selected" in h for h in listener.hints)

    # Frozen leaf: stage something, navigate onto the (now frozen) root, submit → hint, stay put.
    area.toggle_commit_selection(root, item)
    await area.branch(name="alt")                         # freezes root, cursor → alt
    area.set_cursor(graph.root_cursor())                  # back onto the frozen root
    area.submit_commit()
    assert area.commit_active and area.commit_selection_count == 1
    assert any("frozen" in h for h in listener.hints)


# ------------------------------------------------------------------------------------------------
# Entry points: /commit + input submission while in the mode
# ------------------------------------------------------------------------------------------------

async def test_commit_slash_command_enters_the_mode():
    area = make_area()
    await area.commands.execute("/commit")
    assert area.commit_active


async def test_input_submission_in_commit_mode_routes_to_submit_commit():
    area = make_area()
    node = area.cursor.node
    item = area.append_item(agent_msg("a"))
    area.enter_commit_mode()
    area.toggle_commit_selection(node, item)

    area.chat_input.set_buffer("be brief")
    area.chat_input.submit()                              # COMMIT state fires OnSubmitted → submit_commit

    assert not area.commit_active                         # handed off, mode left
    turns = [p for p in node.queued if isinstance(p, MessagePayload) and p.role == MessagePayload.Role.USER]
    assert any(p.data == "be brief" for p in turns)
    await wait_idle(node)
