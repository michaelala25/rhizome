"""Workspace view: the orchestrator → panel integration.

Mounting a Workspace runs the model's bootstrap and docks the chat area into the center slot. The resource
loader is on demand: ``/resources`` (here driven via ``toggle``) mounts it into the left slot, which
expands; toggling it shut empties and re-collapses the slot. The loader auto-loads from the DB on mount,
so this uses a schema'd in-memory factory (empty library is fine).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from textual.app import App, ComposeResult

from rhizome.app.resource_loader import ResourceLoaderModel
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


async def test_workspace_mounts_chat_area_and_toggles_the_resource_loader():
    accessor = make_root_accessor(session_factory=await _schema_factory())
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()

        ws = pilot.app.query_one(Workspace)
        center = ws.query_one("#slot-center", PanelSlot)
        left = ws.query_one("#slot-left", PanelSlot)

        # bootstrap docks only the chat area; slot-left is empty and collapsed.
        assert isinstance(center.current, ChatArea)
        assert center.current.model is ws.model.chat_area
        assert left.current is None
        assert not left.display

        # /resources opens the loader into slot-left, which expands.
        ws.model.toggle(ResourceLoaderModel)
        await pilot.pause()
        await pilot.pause()  # let the loader's load() resolve
        assert isinstance(left.current, ResourceLoader)
        assert left.current.model is ws.model.resource_loader
        assert left.display

        # /resources again closes it; slot-left empties and collapses.
        ws.model.toggle(ResourceLoaderModel)
        await pilot.pause()
        assert left.current is None
        assert not left.display
