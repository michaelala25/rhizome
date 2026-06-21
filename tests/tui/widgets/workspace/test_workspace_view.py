"""Workspace view: the orchestrator → panel integration.

Mounting a Workspace should run the model's bootstrap and route the surfaced chat-area VM into the
center ``PanelSlot`` — i.e. the panel registration + slot wiring + bootstrap all line up.
"""

from textual.app import App, ComposeResult

from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.panel_orchestrator import PanelSlot
from rhizome.tui.widgets.workspace.workspace import Workspace

from tests.app.test_workspace import make_root_accessor


class _Harness(App):
    def __init__(self, services) -> None:
        super().__init__()
        self._services = services

    def compose(self) -> ComposeResult:
        yield Workspace(services=self._services)


async def test_workspace_mounts_the_chat_area_into_the_center_slot():
    async with _Harness(make_root_accessor()).run_test() as pilot:
        await pilot.pause()

        ws = pilot.app.query_one(Workspace)
        center = ws.query_one("#slot-center", PanelSlot)

        # bootstrap surfaced the chat area; the orchestrator docked its view into the center slot.
        assert isinstance(center.current, ChatArea)
        assert center.current.model is ws.model.chat_area
