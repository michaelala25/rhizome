"""WorkspaceModel: the orchestrator hookup — workspace service scope, per-workspace runtime, and the
chat-area panel built + surfaced at construction. Real stack (runtime + graph) over a fake root agent,
mirroring the chat_area tests."""

from rhizome.agent_new.checkpointer import AgentCheckpointerService, build_checkpointer
from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.factory import AgentFactory, AgentFactoryService
from rhizome.agent_new.state import RootAgentState
from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.commands import CommandRegistry, CommandRegistryService
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.app.workspace.workspace import WorkspaceModel
from rhizome.utils.services import ServiceAccessor

from tests.agent_new.fakes import EchoModel, make_build


def make_root_accessor() -> ServiceAccessor:
    """A root container with the upstream services a workspace expects: Root options, the shared
    checkpointer, and an agent factory carrying a fake ``root`` agent. The workspace declares its own
    runtime at its child scope."""
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
    return accessor


def test_workspace_bootstrap_surfaces_the_chat_area():
    ws = WorkspaceModel(make_root_accessor())
    assert ws.surfaced_view_models() == ()               # construction is cheap: nothing surfaced yet

    ws.bootstrap()
    chat = ws.chat_area
    assert isinstance(chat, ChatAreaModel)
    assert ws.surfaced_view_models() == (chat,)          # bootstrap surfaces the center panel
    assert ws.chat_area is chat                            # custody: one cached instance


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
