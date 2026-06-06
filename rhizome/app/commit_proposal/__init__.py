"""CommitProposal VMs — review surface for a batch of pending knowledge-entry writes.

The package is structured as a small VM tree:
  - ``CommitProposalModel`` (parent): owns the entry list, cursor, exclusion set, edit-instructions
    buffer, and lifecycle (EDITING → DONE).
  - ``EntryDetailsModel`` (child): per-entry buffered edit of title/content. Mirrors the browser's
    entry-details VM, but writes back into the in-memory ``Entry`` dataclass instead of the DB.
  - ``Entry`` / ``EntryType``: plain dataclass + enum for the proposal items. Topic is carried as
    a denormalized ``(topic_id, topic_name)`` pair — no ORM coupling, no topic-map lookup table.

The interrupt-flavored variant (``CommitProposalInterruptModel``) lives in
``rhizome.app.chat_pane.interrupts.commit_proposal``.
"""

from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel
from rhizome.app.commit_proposal.entry import Entry, EntryType
from rhizome.app.commit_proposal.entry_details import EntryDetailsModel

__all__ = ["CommitProposalModel", "Entry", "EntryType", "EntryDetailsModel"]
