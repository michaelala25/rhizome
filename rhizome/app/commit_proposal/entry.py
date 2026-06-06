"""Entry dataclass + ``EntryType`` enum for the commit-proposal surface.

The proposal carries denormalized ``topic_id`` + ``topic_name`` rather than a Topic ORM reference
or a separate topic-map lookup. The view only ever displays the name, so the dumbest viable shape
is to carry it inline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class EntryType(str, Enum):
    FACT = "fact"
    EXPOSITION = "exposition"
    OVERVIEW = "overview"


# Cycle order for the per-entry ``f`` keybinding. ``EntryType.cycle(t)`` and ``cycle(t, forward=False)``
# wrap around in either direction.
_TYPE_CYCLE: tuple[EntryType, ...] = (
    EntryType.FACT,
    EntryType.EXPOSITION,
    EntryType.OVERVIEW,
)


def cycle_entry_type(current: EntryType, *, forward: bool = True) -> EntryType:
    step = 1 if forward else -1
    i = _TYPE_CYCLE.index(current)
    return _TYPE_CYCLE[(i + step) % len(_TYPE_CYCLE)]


@dataclass
class Entry:
    """A single pending knowledge-entry write in a commit proposal.

    ``topic_id`` / ``topic_name`` are denormalized — set both, or set neither (None). The view treats
    a None topic as "untopicked" and renders accordingly.
    """

    title: str
    content: str
    entry_type: EntryType
    topic_id: int | None
    topic_name: str | None

    def clone(self) -> "Entry":
        """Field-by-field copy. Used by ``CommitProposalModel`` to snapshot the initial proposal so
        ``reset()`` can restore it after edits."""
        return replace(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        """Build from a loosely-typed dict (e.g. a commit-subagent payload). Tolerates missing
        fields with sensible defaults."""
        return cls(
            title=d.get("title", ""),
            content=d.get("content", ""),
            entry_type=EntryType(d.get("entry_type", "fact")),
            topic_id=d.get("topic_id"),
            topic_name=d.get("topic_name"),
        )
