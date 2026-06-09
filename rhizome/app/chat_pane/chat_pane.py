"""ChatPaneModel — the chat pane's orchestrator VM.

Composes the conversation (``ConversationAreaModel``) and owns the workspace-level pieces that sit
*around* it: the shared ``ResourceManager`` and the side-panel ``ResourceViewerModel``. The manager
is injected down into the conversation so the agent's loaded resources stay in sync with what the
panel shows.

The conversation escalates workspace actions it can't perform itself — open/close tabs, quit,
toggle the resource viewer, rename the enclosing tab — to the ``ChatPane`` view as Textual
messages; this VM holds no notify channels of its own. It does forward ``append_message`` to the
conversation so ``MainScreen``'s compatibility shim keeps working through the orchestrator.

This is the seam where future regions (graph visualizer, file reader, additional side panels) get
composed alongside the conversation.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rhizome.resources.manager import ResourceManager
from rhizome.app.model import ViewModelBase
from rhizome.app.resource_viewer import ResourceViewerModel
from rhizome.app.chat_pane.conversation_area import ConversationAreaModel
from rhizome.app.chat_pane.conversation_graph import ConversationGraphCursor
from rhizome.app.chat_pane.messages.static import ChatMessageModel


class ChatPaneModel(ViewModelBase):

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        show_welcome: bool = False,
    ) -> None:
        super().__init__()

        self._session_factory = session_factory

        # Shared resource substrate: the agent (inside the conversation) and the side-panel resource
        # viewer both read/write the same ``ResourceManager``, so it's owned here and injected down.
        # ``None`` without a session (test / headless).
        self.resource_manager: ResourceManager | None = (
            ResourceManager(session_factory=session_factory) if session_factory else None
        )

        # Side-panel resource viewer. Owned here (not by the view) so its load/link/cursor state
        # survives toggling the panel open and closed — the view mounts/unmounts the widget against
        # this persistent VM. ``None`` without a session, mirroring ``resource_manager``.
        self.resource_viewer: ResourceViewerModel | None = (
            ResourceViewerModel(session_factory, manager=self.resource_manager)
            if session_factory
            else None
        )

        # The conversation itself. Gets the shared manager so agent-loaded resources reach the panel.
        self.conversation_area = ConversationAreaModel(
            session_factory,
            resource_manager=self.resource_manager,
            show_welcome=show_welcome,
        )

    # ------------------------------------------------------------------
    # MainScreen-facing shim — forwarded to the conversation.
    # ------------------------------------------------------------------

    def append_message(
        self,
        msg: ChatMessageModel,
        *,
        include_in_agent_context: bool = True,
        cursor: ConversationGraphCursor | None = None,
    ) -> None:
        self.conversation_area.append_message(
            msg, include_in_agent_context=include_in_agent_context, cursor=cursor
        )
