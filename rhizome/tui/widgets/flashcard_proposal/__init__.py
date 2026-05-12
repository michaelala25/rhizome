"""flashcard_proposal package — legacy widget + in-progress MVVM rewrite.

The legacy widget at ``._legacy.FlashcardProposal`` is the one currently wired
into ``agent_message_harness`` and ``chat_pane``; it's re-exported here as
``FlashcardProposal`` so existing call sites keep resolving to the working
implementation.

The MVVM rewrite lives in ``.view`` (``FlashcardProposal``) and ``.view_model``
(``FlashcardProposalViewModel``). It mirrors ``commit_proposal``'s structure —
VM owns model + widget state, view owns layout / focus / key routing.
"""

from ._legacy import FlashcardProposal
from .view import FlashcardProposal as FlashcardProposalMVVM

__all__ = ["FlashcardProposal", "FlashcardProposalMVVM"]
