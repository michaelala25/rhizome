"""ChatArea view: the depth-nested feed render.

The repo doesn't otherwise unit-test Textual views (they're verified by running the app), but the
DepthWrapper mount-ordering is intricate enough to pin down here with a minimal ``run_test`` harness —
mount the view over a real ChatAreaModel (fake model), drive a branch through the VM, and assert the
branch's feed lands under a depth wrapper while the root feed + indicator stay at the top level.
"""

from textual.app import App, ComposeResult
from textual.widgets import Static

from rhizome.agent.app_context import VALID_VERBOSITIES
from rhizome.agent.state import RootAgentState
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.tui.widgets.chat_area.branch import BranchPoint
from rhizome.tui.widgets.chat_area.chat_area import ChatArea, DepthWrapper
from rhizome.tui.widgets.chat_area.status import StatusBar
from rhizome.tui.types import Mode, Role

from tests.agent.fakes import EchoModel, build_runtime


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


async def test_status_bar_reflects_mode_and_verbosity():
    """The docked StatusBar repaints from the VM's status_bar projection: a mode/verbosity change on
    the VM (writing the leaf's AppContextStore) lands in the bar's rendered text."""
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        await pilot.pause()
        vm.set_mode(Mode.LEARN)
        vm.set_verbosity("verbose")
        await pilot.pause()

        rendered = pilot.app.query_one(StatusBar).query_one(Static).content.plain
        assert "learn" in rendered
        assert "verbose" in rendered


async def test_cycle_actions_advance_mode_and_verbosity():
    """The view owns the cycle order; the handlers read current state and call the VM's setters.
    Mode cycling is silent (no feed message) — the status bar is the only surface that reflects it."""
    vm = make_vm()
    async with _Harness(vm).run_test() as pilot:
        await pilot.pause()
        chat = pilot.app.query_one(ChatArea)

        assert vm.mode is Mode.IDLE
        chat.action_cycle_mode()
        assert vm.mode is Mode.LEARN
        chat.action_cycle_mode()
        assert vm.mode is Mode.REVIEW
        chat.action_cycle_mode()
        assert vm.mode is Mode.IDLE
        assert vm.cursor.node.feed == []          # silent: cycling posts nothing to the feed

        # Verbosity advances through the vocabulary, wrapping ("auto" is last → "terse").
        assert vm.verbosity == "auto"
        chat.action_cycle_verbosity()
        assert vm.verbosity == "terse"

        # ctrl+b is a priority binding, so it fires while the chat input holds focus.
        await pilot.press("ctrl+b")
        assert vm.verbosity == VALID_VERBOSITIES[VALID_VERBOSITIES.index("terse") + 1]
