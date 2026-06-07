"""``CommitProposalModel`` — VM for the commit-proposal review surface.

State machine
-------------
``REVIEWING`` is the resting state: the user is tweaking entries (inline title/content edits,
exclusions, type cycle, topic reassignment) and may either ``accept()`` or transition to
``REQUESTING_REVISION`` via ``request_revision()`` to ask the agent to redo the proposal with
natural-language feedback.

``REQUESTING_REVISION`` is the same as ``REVIEWING`` for per-entry mutators (inline edits remain
valid — the agent's revision tool consumes both the user's edits and the feedback together) but
gates the terminal action to ``submit_revision(feedback)``. ``cancel_revision()`` returns to
``REVIEWING`` without ending the proposal.

``DONE`` is terminal. ``outcome`` reports what was decided (``ACCEPTED`` / ``REVISED`` /
``CANCELLED``); ``revision_feedback`` carries the feedback text iff ``outcome is REVISED``.

Buffer ownership
----------------
The view holds the editable buffers for entry title/content and for revision feedback. The VM
receives finalised values at confirm points (``set_entry_title`` after the entry-details Accept
gesture, ``submit_revision(text)`` when the revision menu's Submit fires). The VM never round-
trips per-keystroke text changes.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any

from rhizome.app.model import ViewModelBase
from rhizome.db import Topic


class EntryType(str, Enum):
    FACT = "fact"
    EXPOSITION = "exposition"
    OVERVIEW = "overview"


@dataclass
class Entry:
    """A single pending knowledge-entry write in a commit proposal."""

    title: str
    content: str
    entry_type: EntryType
    topic: Topic | None

    def clone(self) -> "Entry":
        """Field-by-field copy. Used by ``CommitProposalModel`` to snapshot the initial proposal
        for ``reset``. Shallow on ``topic`` — Topic instances are treated as immutable references."""
        return replace(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any], topic: Topic | None = None) -> "Entry":
        return cls(
            title=d.get("title", ""),
            content=d.get("content", ""),
            entry_type=EntryType(d.get("entry_type", "fact")),
            topic=topic,
        )


_NEXT_TYPE = {
    EntryType.FACT:       EntryType.EXPOSITION,
    EntryType.EXPOSITION: EntryType.OVERVIEW,
    EntryType.OVERVIEW:   EntryType.FACT,
}


class CommitProposalModel(ViewModelBase):

    class State(Enum):
        REVIEWING           = auto()
        REQUESTING_REVISION = auto()
        DONE                = auto()

    class Outcome(Enum):
        ACCEPTED  = auto()
        REVISED   = auto()
        CANCELLED = auto()

    class Callbacks(ViewModelBase.Callbacks):
        OnEntriesChanged  = "OnEntriesChanged"
        OnRevisingChanged = "OnRevisingChanged"
        OnDone            = "OnDone"

    def __init__(self, entries: list[Entry], *, session_factory: Any = None) -> None:
        super().__init__()

        self.session_factory = session_factory

        self._initial: list[Entry] = [deepcopy(e) for e in entries]
        self.entries:  list[Entry] = [deepcopy(e) for e in entries]
        self.excluded: set[int]    = set()

        self._state:             CommitProposalModel.State            = CommitProposalModel.State.REVIEWING
        self._outcome:           CommitProposalModel.Outcome | None   = None
        self._revision_feedback: str | None                           = None

        self.make_callback_groups({
            self.Callbacks.OnEntriesChanged:  list[int],
            self.Callbacks.OnRevisingChanged: bool,
            self.Callbacks.OnDone:            CommitProposalModel.Outcome,
        })

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> "CommitProposalModel.State":
        return self._state

    @property
    def outcome(self) -> "CommitProposalModel.Outcome | None":
        return self._outcome

    @property
    def revision_feedback(self) -> str | None:
        return self._revision_feedback

    @property
    def is_done(self) -> bool:
        return self._state == CommitProposalModel.State.DONE

    @property
    def is_revising(self) -> bool:
        return self._state == CommitProposalModel.State.REQUESTING_REVISION

    @property
    def cancelled(self) -> bool:
        return self._outcome is CommitProposalModel.Outcome.CANCELLED

    @property
    def accepted_entries(self) -> list[Entry]:
        return [deepcopy(e) for i, e in enumerate(self.entries) if i not in self.excluded]

    def is_excluded(self, idx: int) -> bool:
        return idx in self.excluded

    # ------------------------------------------------------------------
    # Per-entry mutators — valid in REVIEWING and REQUESTING_REVISION
    # ------------------------------------------------------------------

    def set_excluded(self, idx: int, excluded: bool) -> None:
        self._assert_open()

        if excluded == (idx in self.excluded):
            return

        if excluded:
            self.excluded.add(idx)
        else:
            self.excluded.discard(idx)

        self.emit(self.Callbacks.OnEntriesChanged, [idx])

    def toggle_excluded(self, idx: int) -> bool:
        new = not self.is_excluded(idx)
        self.set_excluded(idx, new)
        return new

    def set_entry_type(self, idx: int, entry_type: EntryType) -> None:
        self._assert_open()
        entry = self.entries[idx]

        if entry.entry_type == entry_type:
            return

        entry.entry_type = entry_type
        self.emit(self.Callbacks.OnEntriesChanged, [idx])

    def cycle_entry_type(self, idx: int) -> EntryType:
        self.set_entry_type(idx, _NEXT_TYPE[self.entries[idx].entry_type])
        return self.entries[idx].entry_type

    def set_entry_topic(self, idx: int, topic: Topic) -> None:
        self._assert_open()
        entry = self.entries[idx]

        if entry.topic is not None and entry.topic.id == topic.id:
            return

        entry.topic = topic
        self.emit(self.Callbacks.OnEntriesChanged, [idx])

    def set_topic_all(self, topic: Topic) -> None:
        self._assert_open()

        dirty: list[int] = []
        for i, e in enumerate(self.entries):
            if e.topic is None or e.topic.id != topic.id:
                e.topic = topic
                dirty.append(i)

        if dirty:
            self.emit(self.Callbacks.OnEntriesChanged, dirty)

    def set_entry_title(self, idx: int, text: str) -> None:
        self._assert_open()
        entry = self.entries[idx]

        if entry.title == text:
            return

        entry.title = text
        self.emit(self.Callbacks.OnEntriesChanged, [idx])

    def set_entry_content(self, idx: int, text: str) -> None:
        self._assert_open()
        entry = self.entries[idx]

        if entry.content == text:
            return

        entry.content = text
        self.emit(self.Callbacks.OnEntriesChanged, [idx])

    # ------------------------------------------------------------------
    # Revision lifecycle
    # ------------------------------------------------------------------

    def request_revision(self) -> None:
        assert self._state == CommitProposalModel.State.REVIEWING

        self._state = CommitProposalModel.State.REQUESTING_REVISION
        self.emit(self.Callbacks.OnRevisingChanged, True)

    def cancel_revision(self) -> None:
        assert self._state == CommitProposalModel.State.REQUESTING_REVISION

        self._state = CommitProposalModel.State.REVIEWING
        self.emit(self.Callbacks.OnRevisingChanged, False)

    def submit_revision(self, feedback: str) -> None:
        assert self._state == CommitProposalModel.State.REQUESTING_REVISION

        self._revision_feedback = feedback
        self._state = CommitProposalModel.State.DONE
        self._outcome = CommitProposalModel.Outcome.REVISED
        self.emit(self.Callbacks.OnDone, self._outcome)

    # ------------------------------------------------------------------
    # Terminal lifecycle
    # ------------------------------------------------------------------

    def accept(self) -> None:
        assert self._state == CommitProposalModel.State.REVIEWING

        self._state = CommitProposalModel.State.DONE
        self._outcome = CommitProposalModel.Outcome.ACCEPTED
        self.emit(self.Callbacks.OnDone, self._outcome)

    def cancel(self) -> None:
        self._assert_open()

        self._state = CommitProposalModel.State.DONE
        self._outcome = CommitProposalModel.Outcome.CANCELLED
        self.emit(self.Callbacks.OnDone, self._outcome)

    def reset(self) -> None:
        self._assert_open()

        dirty = {i for i, e in enumerate(self.entries) if self._initial[i] != e}
        dirty |= self.excluded

        self.entries = [deepcopy(e) for e in self._initial]
        self.excluded.clear()

        if dirty:
            self.emit(self.Callbacks.OnEntriesChanged, sorted(dirty))

        if self._state == CommitProposalModel.State.REQUESTING_REVISION:
            self._state = CommitProposalModel.State.REVIEWING
            self.emit(self.Callbacks.OnRevisingChanged, False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_open(self) -> None:
        assert self._state != CommitProposalModel.State.DONE, (
            "Cannot mutate a CommitProposalModel after it reaches DONE."
        )
