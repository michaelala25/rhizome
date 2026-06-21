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

from rhizome.db import SessionFactoryService
from rhizome.utils.services import ServiceAccessor
from rhizome.resources.embeddings import EmbeddingService
from rhizome.resources.manager import ResourceManager
from rhizome.app.model import ViewModelBase
from rhizome.app.resource_viewer import ResourceViewerModel
from rhizome.app.chat_pane.conversation_area import ConversationAreaModel
from rhizome.app.chat_pane.conversation_graph import ConversationGraphCursor
from rhizome.app.chat_pane.messages.static import ChatMessageModel


class ChatPaneModel(ViewModelBase):

    def __init__(
        self,
        services: ServiceAccessor,
        *,
        show_welcome: bool = False,
    ) -> None:
        super().__init__()

        # The accessor is the spine's currency: stored and passed to the child VMs, which open their
        # own child scopes as needed. SessionFactoryService is required -- a missing registration is a
        # wiring bug, not a headless mode -- so resolve it with get().
        self._services = services
        self._session_factory = services.get(SessionFactoryService)

        # Shared resource substrate: the agent (inside the conversation) and the side-panel resource
        # viewer both read/write the same ``ResourceManager``, so it's owned here and injected down.
        self.resource_manager: ResourceManager = ResourceManager(
            session_factory=self._session_factory,
            embedding_service=self._services.try_get(EmbeddingService),
        )

        # Side-panel resource viewer. Owned here (not by the view) so its load/link/cursor state
        # survives toggling the panel open and closed — the view mounts/unmounts the widget against
        # this persistent VM.
        self.resource_viewer: ResourceViewerModel = ResourceViewerModel(
            self._services, manager=self.resource_manager
        )

        # The conversation itself. Gets the shared manager so agent-loaded resources reach the panel.
        self.conversation_area = ConversationAreaModel(
            self._services,
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
