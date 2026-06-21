"""chat_pane package — MVVM chat pane.

``ChatPane`` (in ``.chat_pane``) is the orchestrator: it composes the conversation
(``ConversationArea``, in ``.conversation_area``) and docks side panels like the resource viewer.
"""

from rhizome.tui.widgets.chat_pane.chat_pane import ChatPane
from rhizome.tui.widgets.chat_pane.conversation_area import ConversationArea
from rhizome.app.chat_pane.chat_pane import ChatPaneModel
from rhizome.app.chat_pane.conversation_area import ConversationAreaModel

__all__ = ["ChatPane", "ChatPaneModel", "ConversationArea", "ConversationAreaModel"]
