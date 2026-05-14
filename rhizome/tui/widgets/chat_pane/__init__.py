"""chat_pane package — legacy widget + in-progress MVVM rewrite.

The legacy widget at ``._legacy.ChatPane`` is the production
implementation; it's re-exported as ``ChatPane`` (along with
``HintHigherVerbosity``) so existing imports
(``from rhizome.tui.widgets.chat_pane import ChatPane``) keep resolving
to the working widget while the rewrite proceeds.

The MVVM rewrite lives in ``.view`` and ``.view_model``; it covers only
the step 1 slice from ``view_model.md`` (feed + ordinary chat messages)
and is exposed under ``ChatPaneMVVM`` for opt-in use.
"""

from ._legacy import ChatPane, HintHigherVerbosity
from .view import ChatPaneMVVM
from .view_model import ChatPaneViewModel

__all__ = ["ChatPane", "ChatPaneMVVM", "ChatPaneViewModel", "HintHigherVerbosity"]
