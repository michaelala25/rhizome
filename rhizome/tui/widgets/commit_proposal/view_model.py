"""CommitProposalViewModel — data model + widget state, no view concerns.

The VM owns:
  - the entries being proposed and the topic map they reference,
  - which entries the user has chosen to exclude,
  - a cursor over the entry list (so per-cursor mutators don't need the
    caller to pass an index every call),
  - whether the edit-instructions area is showing, and its buffer,
  - a coarse state (still editing vs. resolved) and a ``cancelled`` flag
    distinguishing accepted vs. cancelled DONE, plus a ``collapsed`` view-only
    flag (toggled only in DONE) for the view to fold the widget post-resolve.

It does NOT own:
  - which Textual widget is focused,
  - keyboard routing between regions,
  - which choices to render,
  - any modal-screen plumbing.

Those all live in the view.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any, TYPE_CHECKING

from rhizome.app.vm import ViewModelBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


class EntryType(str, Enum):
    FACT = "fact"
    EXPOSITION = "exposition"
    OVERVIEW = "overview"


_TYPE_CYCLE: list[EntryType] = [
    EntryType.FACT,
    EntryType.EXPOSITION,
    EntryType.OVERVIEW,
]


@dataclass
class Entry:
    title: str
    content: str
    entry_type: EntryType
    topic_id: int | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        return cls(
            title=d.get("title", ""),
            content=d.get("content", ""),
            entry_type=EntryType(d.get("entry_type", "fact")),
            topic_id=d.get("topic_id"),
        )


class CommitProposalViewModel(ViewModelBase):

    class State(Enum):
        EDITING = auto()
        DONE = auto()

    def __init__(
        self,
        entries: list[dict[str, Any]],
        topic_map: dict[int, str],
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ) -> None:
        super().__init__()
        self._initial: list[Entry] = [Entry.from_dict(e) for e in entries]
        self.entries: list[Entry] = [replace(e) for e in self._initial]
        self.topic_map: dict[int, str] = dict(topic_map)
        self.excluded: set[int] = set()
        self.cursor: int | None = 0 if self.entries else None
        self.state: CommitProposalViewModel.State = CommitProposalViewModel.State.EDITING
        self._cancelled: bool = False
        self._collapsed: bool = False
        self.edit_instructions_visible: bool = False
        self.edit_instructions: str = ""
        self.session_factory = session_factory

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @collapsed.setter
    def collapsed(self, value: bool) -> None:
        if self._collapsed == value:
            return
        self._collapsed = value
        self.emit(self.dirty)

    def toggle_collapsed(self) -> None:
        assert self.state == CommitProposalViewModel.State.DONE
        self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_current_entry_cursor(self, idx: int | None) -> None:
        if idx is None or not self.entries:
            self.cursor = None if not self.entries else self.cursor
            if idx is None:
                self.cursor = None
        else:
            self.cursor = max(0, min(idx, len(self.entries) - 1))
        self.emit(self.dirty)

    def next_entry(self) -> bool:
        """Advance the cursor one entry. Returns False if already at the end (or list is empty) so the
        view can decide what to do at the boundary."""
        if not self.entries:
            return False
        if self.cursor is None:
            self.cursor = 0
            self.emit(self.dirty)
            return True
        if self.cursor >= len(self.entries) - 1:
            return False
        self.cursor += 1
        self.emit(self.dirty)
        return True

    def prev_entry(self) -> bool:
        """Step the cursor back one entry. Returns False at the boundary."""
        if not self.entries or self.cursor is None:
            return False
        if self.cursor <= 0:
            return False
        self.cursor -= 1
        self.emit(self.dirty)
        return True

    # ------------------------------------------------------------------
    # Entry field mutators — index-explicit, plus convenience wrappers
    # ------------------------------------------------------------------

    def set_entry_title(self, idx: int, title: str) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.entries[idx].title == title:
            return
        self.entries[idx].title = title
        self.emit(self.dirty)

    def set_entry_content(self, idx: int, content: str) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.entries[idx].content == content:
            return
        self.entries[idx].content = content
        self.emit(self.dirty)

    def set_entry_type(self, idx: int, entry_type: EntryType) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.entries[idx].entry_type == entry_type:
            return
        self.entries[idx].entry_type = entry_type
        self.emit(self.dirty)

    def set_entry_topic(self, idx: int, topic_id: int) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.entries[idx].topic_id == topic_id:
            return
        self.entries[idx].topic_id = topic_id
        self.emit(self.dirty)

    def cycle_current_entry_type(self, *, forward: bool = True) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.cursor is None:
            return
        cur = self.entries[self.cursor].entry_type
        step = 1 if forward else -1
        i = _TYPE_CYCLE.index(cur)
        self.entries[self.cursor].entry_type = _TYPE_CYCLE[
            (i + step) % len(_TYPE_CYCLE)
        ]
        self.emit(self.dirty)

    def set_topic_all(self, topic_id: int) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if all(e.topic_id == topic_id for e in self.entries):
            return
        for e in self.entries:
            e.topic_id = topic_id
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Exclusion
    # ------------------------------------------------------------------

    def toggle_exclude_current_entry(self) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.cursor is None:
            return
        if self.cursor in self.excluded:
            self.excluded.remove(self.cursor)
        else:
            self.excluded.add(self.cursor)
        self.emit(self.dirty)

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    # ------------------------------------------------------------------
    # Edit-instructions area
    # ------------------------------------------------------------------

    def toggle_edit_instructions_area(self) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        self.edit_instructions_visible = not self.edit_instructions_visible
        self.emit(self.dirty)

    def set_edit_instructions(self, text: str) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        if self.edit_instructions == text:
            return
        self.edit_instructions = text
        self.emit(self.dirty)

    def discard_edit_instructions(self) -> None:
        """Clear the buffer and hide the area."""
        assert self.state == CommitProposalViewModel.State.EDITING
        self.edit_instructions = ""
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def accept(self) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        self.state = CommitProposalViewModel.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def cancel(self) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        self._cancelled = True
        self.state = CommitProposalViewModel.State.DONE
        self._collapsed = True
        self.emit(self.dirty)

    def reset(self) -> None:
        assert self.state == CommitProposalViewModel.State.EDITING
        self.entries = [replace(e) for e in self._initial]
        self.excluded.clear()
        if not self.entries:
            self.cursor = None
        elif self.cursor is not None:
            self.cursor = min(self.cursor, len(self.entries) - 1)
        self.edit_instructions = ""
        self.edit_instructions_visible = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Topic-map maintenance — parent calls this after a DB commit that may have changed topic rows.
    # ------------------------------------------------------------------

    def replace_topic_map(self, topic_map: dict[int, str]) -> None:
        self.topic_map = dict(topic_map)
        self.emit(self.dirty)

    def topic_name(self, topic_id: int | None) -> str | None:
        if topic_id is None:
            return None
        return self.topic_map.get(topic_id)

    def has_stale_topics(self) -> bool:
        return any(
            e.topic_id is not None and e.topic_id not in self.topic_map
            for i, e in enumerate(self.entries)
            if i not in self.excluded
        )
