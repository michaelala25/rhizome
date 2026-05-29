"""``Flashcard`` dataclass for the flashcard-proposal surface.

Mirrors ``rhizome.app.commit_proposal.entry.Entry`` in shape. The proposal carries denormalized
``topic_id`` + ``topic_name`` rather than a ``Topic`` ORM reference — the view only ever displays
the name and the dumbest viable shape is to carry it inline.

``entry_ids`` is the list of knowledge-entry IDs that the flashcard tests against; it is shown
read-only in the details panel and is never mutated by the user inside the proposal widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class Flashcard:
    """A single pending flashcard write in a flashcard proposal.

    ``topic_id`` / ``topic_name`` are denormalized — set both, or set neither (None). The view
    treats a None topic as "untopicked" and renders accordingly.

    ``entry_ids`` is treated as immutable from the widget's perspective: the user cannot edit the
    list of linked knowledge entries inside the proposal review surface. ``clone()`` still copies
    the list so two clones don't alias.
    """

    question: str
    answer: str
    testing_notes: str
    topic_id: int | None
    topic_name: str | None
    entry_ids: list[int] = field(default_factory=list)

    def clone(self) -> "Flashcard":
        """Field-by-field copy. Used by ``FlashcardProposalVM`` to snapshot the initial proposal so
        ``reset()`` can restore it after edits. The ``entry_ids`` list is copied so the snapshot
        and the working list don't share mutable state."""
        return replace(self, entry_ids=list(self.entry_ids))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Flashcard":
        """Build from a loosely-typed dict (e.g. a flashcard-subagent payload). Tolerates missing
        fields with sensible defaults; ``testing_notes`` of ``None`` collapses to an empty string
        so the buffered-edit comparator sees a single canonical form."""
        return cls(
            question=d.get("question", ""),
            answer=d.get("answer", ""),
            testing_notes=d.get("testing_notes") or "",
            topic_id=d.get("topic_id"),
            topic_name=d.get("topic_name"),
            entry_ids=list(d.get("entry_ids") or []),
        )
