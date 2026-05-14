"""chat_pane package.

The legacy widget has been moved into ``._legacy`` so an MVVM rewrite
can be developed alongside it. ``ChatPane`` (and ``HintHigherVerbosity``)
are re-exported here so existing imports
(``from rhizome.tui.widgets.chat_pane import ChatPane``) keep resolving
to the working widget.
"""

from ._legacy import ChatPane, HintHigherVerbosity

__all__ = ["ChatPane", "HintHigherVerbosity"]
