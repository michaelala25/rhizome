"""chat_pane package — legacy widget + in-progress MVVM rewrite.

The legacy widget lives at ``..legacy.chat_pane.ChatPane``; it's re-exported here
(along with ``HintHigherVerbosity``) so existing imports
(``from rhizome.tui.widgets.chat_pane import ChatPane``) keep resolving to the
working widget while the rewrite proceeds.

The MVVM rewrite lives in ``.view`` and ``.view_model`` and is exposed under
``ChatPaneMVVM`` for opt-in use.
"""

from ..legacy.chat_pane import ChatPane, HintHigherVerbosity
from .view import ChatPaneMVVM
from .view_model import ChatPaneViewModel

__all__ = ["ChatPane", "ChatPaneMVVM", "ChatPaneViewModel", "HintHigherVerbosity"]
