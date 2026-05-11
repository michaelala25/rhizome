"""commit_proposal package — legacy widget + in-progress MVVM rewrite.

The legacy widget at ``._legacy.CommitProposal`` is the one currently wired
into ``agent_message_harness`` and ``chat_pane``; it's re-exported here as
``CommitProposal`` so existing call sites (``from .commit_proposal import
CommitProposal``) keep resolving to the working implementation.

The MVVM rewrite lives in ``.view`` (``CommitProposal``) and ``.view_model``
(``CommitProposalViewModel``). It is not yet feature-complete (no resolution
state, no topic selector wiring) — exposed under a distinct name so callers
opt in explicitly.
"""

from ._legacy import CommitProposal
from .view import CommitProposal as CommitProposalMVVM

__all__ = ["CommitProposal", "CommitProposalMVVM"]
