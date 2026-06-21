"""ChatArea view: the depth-nested feed render.

The repo doesn't otherwise unit-test Textual views (they're verified by running the app), but the
DepthWrapper mount-ordering is intricate enough to pin down here with a minimal ``run_test`` harness —
mount the view over a real ChatAreaModel (fake model), drive a branch through the VM, and assert the
branch's feed lands under a depth wrapper while the root feed + indicator stay at the top level.
"""

from textual.app import App, ComposeResult

from rhizome.agent_new.state import RootAgentState
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.tui.widgets.chat_area.branch import BranchPoint
from rhizome.tui.widgets.chat_area.chat_area import ChatArea, DepthWrapper
from rhizome.tui.types import Role

from tests.agent_new.fakes import EchoModel, build_runtime


class _Harness(App):
    def __init__(self, vm: ChatAreaModel) -> None:
        super().__init__()
        self._vm = vm

    def compose(self) -> ComposeResult:
        yield ChatArea(self._vm)


def make_vm() -> ChatAreaModel:
    return ChatAreaModel(build_runtime(lambda: EchoModel(), state_schema=RootAgentState))


async def test_branch_feed_renders_under_a_depth_wrapper():
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        vm.append_message("root msg", Role.SYSTEM, to_agent=False)
        await pilot.pause()
        await vm.branch(name="alt")          # continuation + "alt"; indicator into root, cursor -> alt
        await pilot.pause()
        vm.append_message("branch msg", Role.SYSTEM, to_agent=False)
        await pilot.pause()

        chat = pilot.app.query_one(ChatArea)
        inner = chat.query_one("#message-area-inner")
        wrappers = list(chat.query(DepthWrapper))

        # One depth level below the root (the checked-out "alt" branch), holding exactly the branch msg.
        assert len(wrappers) == 1
        assert len(wrappers[0].children) == 1
        # The branch indicator sits at the root level, not inside the wrapper.
        assert any(isinstance(w, BranchPoint) for w in inner.children)
        assert not any(isinstance(w, BranchPoint) for w in wrappers[0].children)
