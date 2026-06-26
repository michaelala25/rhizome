"""Workspace view: the orchestrator → panel integration.

Mounting a Workspace runs the model's bootstrap and docks the chat area into the center slot. The resource
loader is on demand: ``/resources`` (here driven via ``toggle``) mounts it into the left slot, which
expands; toggling it shut empties and re-collapses the slot. The loader auto-loads from the DB on mount,
so this uses a schema'd in-memory factory (empty library is fine).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from textual.app import App, ComposeResult
from textual.widget import Widget

from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.app.graph_viewer import GraphViewerModel
from rhizome.app.options import Options, OptionService
from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.db.models import Base
from rhizome.tui.types import Role
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.graph_viewer import GraphViewer
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


async def test_graph_viewer_toggles_and_is_mutually_exclusive_with_the_loader():
    accessor = make_root_accessor(session_factory=await _schema_factory())
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()
        ws = pilot.app.query_one(Workspace)
        left = ws.query_one("#slot-left", PanelSlot)

        # /graph opens the viewer into slot-left, which expands.
        ws.model.toggle(GraphViewerModel)
        await pilot.pause()
        assert isinstance(left.current, GraphViewer)
        assert left.current.model is ws.model.graph_viewer
        assert left.display

        # /resources swaps it for the loader — one left-hand tool at a time, and the surfaced set agrees.
        ws.model.toggle(ResourceLoaderModel)
        await pilot.pause()
        await pilot.pause()  # let the loader's load() resolve
        assert isinstance(left.current, ResourceLoader)
        surfaced = {type(vm) for vm in ws.model.surfaced_view_models()}
        assert ResourceLoaderModel in surfaced and GraphViewerModel not in surfaced

        # closing the loader empties and collapses the slot.
        ws.model.toggle(ResourceLoaderModel)
        await pilot.pause()
        assert left.current is None
        assert not left.display


async def test_quick_nav_from_the_graph_viewer_scrolls_the_chat(monkeypatch):
    # Spy every scroll_visible: the request_scroll_visible seam must reach a chat-area feed widget that
    # was only just (re)mounted by the quick-nav's cursor change — the call_after_refresh timing case.
    scrolls: list = []
    real_scroll_visible = Widget.scroll_visible

    def spy(self, *args, **kwargs):
        scrolls.append((self, kwargs.get("top")))
        return real_scroll_visible(self, *args, **kwargs)

    monkeypatch.setattr(Widget, "scroll_visible", spy)

    accessor = make_root_accessor(session_factory=await _schema_factory())
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()
        ws = pilot.app.query_one(Workspace)
        chat = ws.model.chat_area

        chat.append_item(ChatMessageModel(Role.USER, "root line"))           # root feed (still live)
        new = await chat.branch()                                            # fork; cursor now at `new`
        chat.append_item(ChatMessageModel(Role.USER, "branch top"), cursor=new)
        chat.set_cursor(chat.conversation_graph.root)                        # leave `new` off the path
        await pilot.pause()

        scrolls.clear()
        ws.model.graph_viewer.quick_nav(("node", new.node.id))               # "select" that node
        await pilot.pause()
        await pilot.pause()

        assert chat.cursor.node.id == new.node.id                            # the chat navigated there
        assert any(top is True for _widget, top in scrolls)                  # a feed widget scrolled to top


async def test_ctrl_left_from_chat_input_enters_the_left_panel_when_enabled():
    """The full outer-nav path: with ``CtrlNavFromChatInput`` enabled, Ctrl+Left in the chat input hands
    off (SkipAction) past the chat area to the Workspace's outer graph, which moves focus into the docked
    left panel."""
    accessor = make_root_accessor(session_factory=await _schema_factory())
    accessor.get(OptionService).set(Options.CtrlNavFromChatInput, "enabled", flush=False)
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()
        ws = pilot.app.query_one(Workspace)
        left = ws.query_one("#slot-left", PanelSlot)

        ws.model.toggle(ResourceLoaderModel)                 # dock the loader into slot-left
        await pilot.pause()
        await pilot.pause()                                  # let the loader's load() resolve

        chat_input = ws.query_one("#chat-input")
        chat_input.focus()
        await pilot.pause()
        assert pilot.app.focused is chat_input               # precondition: focus in the input

        await pilot.press("ctrl+left")                       # hand off to outer nav → left panel
        await pilot.pause()
        focused = pilot.app.focused
        assert focused is not None and left.current in focused.ancestors_with_self


async def test_ctrl_right_from_the_left_panel_returns_to_the_chat_area():
    """Reverse outer hop, toggle-independent: the loader binds no left/right, so Ctrl+Right falls through
    it to the Workspace's outer graph, landing focus back in the chat area's input."""
    accessor = make_root_accessor(session_factory=await _schema_factory())
    async with _Harness(accessor).run_test() as pilot:
        await pilot.pause()
        ws = pilot.app.query_one(Workspace)
        left = ws.query_one("#slot-left", PanelSlot)

        ws.model.toggle(ResourceLoaderModel)
        await pilot.pause()
        await pilot.pause()
        left.current.focus()                                 # delegates inward to the loader's tree
        await pilot.pause()

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert pilot.app.focused is ws.query_one("#chat-input")
