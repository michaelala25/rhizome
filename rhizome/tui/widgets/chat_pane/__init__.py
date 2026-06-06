"""chat_pane package — MVVM chat pane.

The legacy widget still lives at ``rhizome.tui.widgets.legacy.chat_pane.ChatPane``
(also re-exported from ``rhizome.tui.widgets``); the new MVVM ``ChatPane`` in
``.view`` takes the unqualified name in this package.
"""

from rhizome.tui.widgets.chat_pane.chat_pane import ChatPane
from rhizome.app.chat_pane.chat_pane import ChatPaneModel

__all__ = ["ChatPane", "ChatPaneModel"]
