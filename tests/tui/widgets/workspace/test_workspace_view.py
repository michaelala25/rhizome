"""Workspace view: the orchestrator → panel integration.

Mounting a Workspace should run the model's bootstrap and route each surfaced panel VM into its slot —
the chat area into the center, the resource loader into the left — i.e. the panel registrations + slot
wiring + bootstrap all line up. The loader auto-loads from the DB on mount, so this uses a schema'd
in-memory factory (empty library is fine).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from textual.app import App, ComposeResult

from rhizome.db.models import Base
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.panel_orchestrator import PanelSlot
from rhizome.tui.widgets.resource_loader import ResourceLoader
from rhizome.tui.widgets.workspace.workspace import Workspace

from tests.app.test_workspace import make_root_accessor


async def _schema_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _Harness(App):
    def __init__(self, services) -> None:
        super().__init__()
        self._services = services

    def compose(self) -> ComposeResult:
        yield Workspace(services=self._services)


async def test_workspace_mounts_panels_into_their_slots():
    accessor = make_root_accessor(session_factory=await _schema_factory())
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()  # let the loader's load() resolve

        ws = pilot.app.query_one(Workspace)
        center = ws.query_one("#slot-center", PanelSlot)
        left = ws.query_one("#slot-left", PanelSlot)

        # bootstrap surfaced both panels; the orchestrator docked each view into its slot.
        assert isinstance(center.current, ChatArea)
        assert center.current.model is ws.model.chat_area
        assert isinstance(left.current, ResourceLoader)
        assert left.current.model is ws.model.resource_loader
