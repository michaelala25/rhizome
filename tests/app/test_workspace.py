"""WorkspaceModel: the orchestrator hookup — workspace service scope, per-workspace runtime, and the
chat-area panel built + surfaced at construction. Real stack (runtime + graph) over a fake root agent,
mirroring the chat_area tests."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.agent.checkpointer import AgentCheckpointerService, build_checkpointer
from rhizome.agent.context import RootAgentContext
from rhizome.agent.factory import AgentFactory, AgentFactoryService
from rhizome.agent.state import RootAgentState
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.commands import CommandRegistry, CommandRegistryService
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.app.workspace.workspace import WorkspaceModel
from rhizome.db import SessionFactoryService
from rhizome.utils.services import ServiceAccessor

from tests.agent.fakes import EchoModel, make_build


def make_session_factory():
    """A bare in-memory async session factory. No schema — fine for the VM-only tests, which construct
    the resource layer but never query (``load`` runs view-side). The view test supplies a schema'd one."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    return async_sessionmaker(engine, expire_on_commit=False)


def make_root_accessor(session_factory=None) -> ServiceAccessor:
    """A root container with the upstream services a workspace expects: Root options, the shared
    checkpointer, an agent factory carrying a fake ``root`` agent, and a session factory. The workspace
    declares its own runtime at its child scope."""
    accessor = ServiceAccessor()
    accessor.register(OptionService, Options(OptionScope.Root))
    accessor.register_descriptor(AgentCheckpointerService, build_checkpointer)
    factory = AgentFactory()
    factory.register(
        "root",
        build=make_build(lambda: EchoModel(), state_schema=RootAgentState),
        context_schema=RootAgentContext,
    )
    accessor.register(AgentFactoryService, factory)
    accessor.register(SessionFactoryService, session_factory or make_session_factory())
    return accessor


def test_workspace_bootstrap_surfaces_its_panels():
    ws = WorkspaceModel(make_root_accessor())
    assert ws.surfaced_view_models() == ()               # construction is cheap: nothing surfaced yet

    ws.bootstrap()
    chat = ws.chat_area
    assert isinstance(chat, ChatAreaModel)
    # bootstrap surfaces both the center (chat) and left (loader) panels.
    assert set(ws.surfaced_view_models()) == {chat, ws.resource_loader}
    assert ws.chat_area is chat                            # custody: one cached instance


async def test_resource_stores_shared_and_local_follows_cursor():
    ws = WorkspaceModel(make_root_accessor())
    ws.bootstrap()
    chat, loader = ws.chat_area, ws.resource_loader
    graph = chat.conversation_graph

    # The loader and the conversation graph share the same graph-global stores.
    assert loader._index is graph.resource_index
    assert loader._global is graph.resource_context
    # The loader's local channel tracks the current conversation leaf.
    assert loader._local is chat.cursor.node.resources

    # Branching moves the cursor; the loader's local channel follows to the new leaf's node-local store.
    await chat.branch(name="alt")
    assert loader._local is chat.cursor.node.resources


def test_each_workspace_gets_its_own_runtime():
    root = make_root_accessor()
    a = WorkspaceModel(root)
    b = WorkspaceModel(root)

    # Two workspaces off one root share the agent factory but get independent (workspace-scoped) runtimes.
    assert a.chat_area.runtime is not b.chat_area.runtime


def test_workspace_registry_parents_to_global_and_backs_the_chat_area():
    root = make_root_accessor()
    global_reg = CommandRegistry()
    global_reg.register("ping", lambda: "pong", help="ping")
    root.register(CommandRegistryService, global_reg)

    reg = WorkspaceModel(root).chat_area.commands

    assert reg.resolve("branch") is not None     # conversation command, registered by the chat area
    assert reg.resolve("ping") is not None        # global command, reached via the parent chain
