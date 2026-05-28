"""chat_pane package — MVVM chat pane.

The legacy widget still lives at ``rhizome.tui.widgets.legacy.chat_pane.ChatPane``
(also re-exported from ``rhizome.tui.widgets``); the new MVVM ``ChatPane`` in
``.view`` takes the unqualified name in this package.
"""

from .view import ChatPane
from .view_model import ChatPaneVM

__all__ = ["ChatPane", "ChatPaneVM"]
