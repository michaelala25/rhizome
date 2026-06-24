"""ChatArea view — commit mode: decoration, the two-graph focus model, and the gated key bindings.

Driven through a real ``run_test`` harness because the load-bearing behaviour is Textual key dispatch:
plain up/down walk a message-only "commit graph", ctrl+up/down re-enter that cluster from the main
graph, and the priority bindings fall through (via ``check_action``) when commit mode isn't the owner —
none of which is observable without the real focus + binding machinery.
"""

from textual.app import App, ComposeResult

from rhizome.agent.state import RootAgentState
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.chat_pane.chat_input import ChatInputModel
from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.types import Mode

from tests.agent.fakes import EchoModel, build_runtime


class _Harness(App):
    def __init__(self, vm: ChatAreaModel) -> None:
        super().__init__()
        self._vm = vm

    def compose(self) -> ComposeResult:
        yield ChatArea(self._vm)


def make_vm() -> ChatAreaModel:
    return ChatAreaModel(build_runtime(lambda: EchoModel(), state_schema=RootAgentState))


def agent_msg(body: str, *, mode: Mode = Mode.IDLE) -> AgentMessageModel:
    vm = AgentMessageModel(mode=mode)
    vm.body = body
    vm.streaming = False
    return vm


async def _wait_idle(pilot, node, ticks: int = 300) -> None:
    for _ in range(ticks):
        if not node.busy:
            return
        await pilot.pause()
    raise TimeoutError("node never went idle")


def _widget(chat: ChatArea, item_id: int):
    return chat.query_one(f"#feed-item-{item_id}")


# ------------------------------------------------------------------------------------------------
# Enter / decorate / focus
# ------------------------------------------------------------------------------------------------

async def test_entering_focuses_the_bottom_message_and_decorates_selectables():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        a = vm.append_item(agent_msg("first"))
        b = vm.append_item(agent_msg("second"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)

        vm.enter_commit_mode()
        await pilot.pause()

        assert vm.chat_input.state == ChatInputModel.State.COMMIT
        assert pilot.app.focused is _widget(chat, b.id)          # entry = bottom-most message
        assert _widget(chat, a.id).has_class("--commit-selectable")
        assert _widget(chat, b.id).has_class("--commit-selectable")


# ------------------------------------------------------------------------------------------------
# Plain up/down: the message-only commit graph
# ------------------------------------------------------------------------------------------------

async def test_plain_up_down_walk_the_messages():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        a = vm.append_item(agent_msg("first"))
        b = vm.append_item(agent_msg("second"))
        c = vm.append_item(agent_msg("third"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)
        vm.enter_commit_mode()
        await pilot.pause()

        assert pilot.app.focused is _widget(chat, c.id)          # bottom
        await pilot.press("up")
        assert pilot.app.focused is _widget(chat, b.id)
        await pilot.press("up")
        assert pilot.app.focused is _widget(chat, a.id)
        await pilot.press("up")                                  # at the top: no move (falls through)
        assert pilot.app.focused is _widget(chat, a.id)
        await pilot.press("down")
        assert pilot.app.focused is _widget(chat, b.id)


async def test_plain_up_in_the_input_does_not_steal_message_nav():
    """The crux of the gating: with the input focused (COMMIT state), plain up must NOT jump into the
    messages — ``check_action`` returns None so the key falls through to the input."""
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        vm.append_item(agent_msg("first"))
        vm.append_item(agent_msg("second"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)
        vm.enter_commit_mode()
        await pilot.pause()

        chat.query_one("#chat-input").focus()
        await pilot.pause()
        await pilot.press("up")
        assert pilot.app.focused is chat.query_one("#chat-input")


# ------------------------------------------------------------------------------------------------
# ctrl+up/down: the main graph re-enters / exits the cluster
# ------------------------------------------------------------------------------------------------

async def test_ctrl_up_enters_cluster_at_entry_and_ctrl_down_returns_to_input():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        a = vm.append_item(agent_msg("first"))
        b = vm.append_item(agent_msg("second"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)
        vm.enter_commit_mode()
        await pilot.pause()

        chat.query_one("#chat-input").focus()
        await pilot.pause()
        await pilot.press("ctrl+up")                             # re-enter the cluster at the entry (b)
        assert pilot.app.focused is _widget(chat, b.id)
        await pilot.press("ctrl+down")                           # exit the cluster back to the input
        assert pilot.app.focused is chat.query_one("#chat-input")


# ------------------------------------------------------------------------------------------------
# space / ctrl+j / esc
# ------------------------------------------------------------------------------------------------

async def test_space_toggles_the_focused_message_selection():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        a = vm.append_item(agent_msg("first"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)
        vm.enter_commit_mode()
        await pilot.pause()
        widget = _widget(chat, a.id)

        await pilot.press("space")
        assert vm.is_committed(a.id) and widget.has_class("--commit-selected")
        await pilot.press("space")
        assert not vm.is_committed(a.id) and not widget.has_class("--commit-selected")


async def test_ctrl_j_submits_and_esc_exits():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        a = vm.append_item(agent_msg("first"))
        await pilot.pause()
        node = vm.cursor.node
        vm.enter_commit_mode()
        await pilot.pause()

        await pilot.press("space")                              # stage the focused message
        assert vm.is_committed(a.id)
        await pilot.press("ctrl+j")                             # priority submit
        await pilot.pause()
        assert not vm.commit_active                             # handed off → mode left
        await _wait_idle(pilot, node)

    # esc exits without submitting
    vm2 = make_vm()
    async with _Harness(vm2).run_test() as pilot:
        vm2.append_item(agent_msg("first"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)
        vm2.enter_commit_mode()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not vm2.commit_active
        assert pilot.app.focused is chat.query_one("#chat-input")


# ------------------------------------------------------------------------------------------------
# Cross-branch: decoration follows the global selection across navigation
# ------------------------------------------------------------------------------------------------

async def test_selection_decoration_persists_across_branch_navigation():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        root_msg = vm.append_item(agent_msg("root answer"))
        await pilot.pause()
        await vm.branch(name="alt")                             # cursor → alt, root frozen
        alt_cursor = vm.cursor
        await pilot.pause()
        alt_msg = vm.append_item(agent_msg("alt answer"))
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)

        vm.enter_commit_mode()
        await pilot.pause()
        await pilot.press("space")                              # stage the alt message (focused on entry)
        assert vm.is_committed(alt_msg.id)

        # Navigate to the root branch: its message is selectable, and not staged.
        vm.set_cursor(vm.conversation_graph.root_cursor())
        await pilot.pause()
        await pilot.pause()
        root_widget = _widget(chat, root_msg.id)
        assert root_widget.has_class("--commit-selectable")
        assert not root_widget.has_class("--commit-selected")

        # Back to alt: the staged decoration survived (the selection is global chat-area state).
        vm.set_cursor(alt_cursor)
        await pilot.pause()
        await pilot.pause()
        assert _widget(chat, alt_msg.id).has_class("--commit-selected")
