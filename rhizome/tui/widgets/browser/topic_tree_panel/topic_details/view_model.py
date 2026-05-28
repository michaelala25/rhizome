"""Buffered-edit VM for the topic details panel (under the topic tree in the browser's left rail).

Editing model: each field has an in-VM buffer seeded from the topic on every fetch. When either
buffer diverges from the loaded topic the VM is ``is_dirty`` and the view reveals an Accept/Cancel
row. Accept persists via ``update_topic`` + commit and mutates the in-memory ``Topic`` in place;
Cancel restores from the topic.

Cursor-move-while-dirty: silent discard. The panel VM calls ``set_topic_id`` on every cursor move
and buffers are unconditionally reseeded once the new topic resolves. While the fetch is in flight
buffers stay on the prior topic — short-lived because ``QueryBackedViewModel`` collapses bursts.

Callback groups:
  * ``dirty`` — standard repaint signal.
  * ``SAVED`` — fires only after a successful Accept. The panel view subscribes so it can repaint
    the tree node's label after a rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from rhizome.db import Topic
from rhizome.db.operations import (
    count_entries,
    count_entries_filtered,
    count_flashcards_by_topic,
    count_flashcards_by_topics,
    expand_subtrees,
    get_topic,
    update_topic,
)
from rhizome.logs import get_logger

from ....query_backed_view_model import QueryBackedViewModel

_logger = get_logger("browser.topic_details")


@dataclass(frozen=True)
class _LoadedTopic:
    """Output of one ``_fetch``: the topic plus the direct/subtree counts that render below the
    description. Bundled so a single fetch carries everything the view needs."""
    topic: Topic | None
    direct_entries: int
    subtree_entries: int
    direct_flashcards: int
    subtree_flashcards: int


class TopicDetailsViewModel(QueryBackedViewModel):
    """Buffered-edit VM for the topic details panel. Accept/Cancel are the explicit exits from dirty;
    nothing reaches the DB until the user Accepts."""

    class Callbacks(Enum):
        SAVED = "saved"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._saved = self._make_group(TopicDetailsViewModel.Callbacks.SAVED)

        self._topic_id: int | None = None
        self._topic: Topic | None = None
        # Buffers shadow the topic's stored values. Reseeded on every successful fetch so the dirty
        # test is a plain string compare with no extra state.
        self._name_buffer: str = ""
        self._description_buffer: str = ""
        # Direct + subtree counts, populated alongside the topic on each fetch. Zero when no topic
        # is loaded.
        self._direct_entries: int = 0
        self._subtree_entries: int = 0
        self._direct_flashcards: int = 0
        self._subtree_flashcards: int = 0

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def saved(self):
        return self._saved

    @property
    def topic(self) -> Topic | None:
        return self._topic

    @property
    def name(self) -> str:
        """The buffer — what the user is currently editing. The view binds the title ``TextArea``
        to this, not to the topic's stored value."""
        return self._name_buffer

    @property
    def description(self) -> str:
        """Buffer; same as ``name``."""
        return self._description_buffer

    @property
    def original_name(self) -> str:
        return "" if self._topic is None else self._topic.name

    @property
    def original_description(self) -> str:
        return "" if self._topic is None else (self._topic.description or "")

    @property
    def direct_entries(self) -> int:
        return self._direct_entries

    @property
    def subtree_entries(self) -> int:
        return self._subtree_entries

    @property
    def direct_flashcards(self) -> int:
        return self._direct_flashcards

    @property
    def subtree_flashcards(self) -> int:
        return self._subtree_flashcards

    @property
    def is_dirty(self) -> bool:
        """True iff either buffer diverges from the loaded topic. False when no topic is loaded."""
        if self._topic is None:
            return False
        return (
            self._name_buffer != self.original_name
            or self._description_buffer != self.original_description
        )

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def set_topic_id(self, topic_id: int | None) -> None:
        """Switch the panel to ``topic_id``. Idempotent on the same id. ``None`` clears
        synchronously (no DB work); otherwise triggers a debounced fetch."""
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        if topic_id is None:
            self._topic = None
            self._name_buffer = ""
            self._description_buffer = ""
            self._direct_entries = 0
            self._subtree_entries = 0
            self._direct_flashcards = 0
            self._subtree_flashcards = 0
            self.emit(self.dirty)
            return
        self._request_fetch()

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def _fetch(self) -> _LoadedTopic:
        topic_id = self._topic_id
        if topic_id is None:
            return _LoadedTopic(None, 0, 0, 0, 0)
        async with self._session_factory() as session:
            topic = await get_topic(session, topic_id)
            if topic is None:
                return _LoadedTopic(None, 0, 0, 0, 0)
            subtree_ids = await expand_subtrees(session, [topic_id])
            direct_entries = await count_entries(session, topic_id)
            subtree_entries = await count_entries_filtered(session, topic_ids=subtree_ids)
            direct_flashcards = await count_flashcards_by_topic(session, topic_id)
            subtree_flashcards = await count_flashcards_by_topics(session, subtree_ids)
        return _LoadedTopic(
            topic=topic,
            direct_entries=direct_entries,
            subtree_entries=subtree_entries,
            direct_flashcards=direct_flashcards,
            subtree_flashcards=subtree_flashcards,
        )

    def _process_fetched_data(self, result: _LoadedTopic) -> None:
        # Discards any in-flight buffer edits on the previous topic — explicit "cursor moved" UX,
        # matching the entry-table's discard-on-nav behaviour.
        topic = result.topic
        self._topic = topic
        self._name_buffer = "" if topic is None else topic.name
        self._description_buffer = "" if topic is None else (topic.description or "")
        self._direct_entries = result.direct_entries
        self._subtree_entries = result.subtree_entries
        self._direct_flashcards = result.direct_flashcards
        self._subtree_flashcards = result.subtree_flashcards

    # ------------------------------------------------------------------
    # Buffer mutators — view-side
    # ------------------------------------------------------------------

    def set_name(self, value: str) -> None:
        # Equality early-return absorbs the round-trip from our own ``_refresh`` assignment.
        if self._topic is None:
            return
        if value == self._name_buffer:
            return
        self._name_buffer = value
        self.emit(self.dirty)

    def set_description(self, value: str) -> None:
        if self._topic is None:
            return
        if value == self._description_buffer:
            return
        self._description_buffer = value
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Accept / Cancel
    # ------------------------------------------------------------------

    async def accept(self) -> None:
        """Persist the buffers and mutate the in-memory topic in place. Emits ``saved`` so the
        panel view can repaint the tree node label. No-ops on empty name (the DB column is
        non-null and an empty rename is almost always an accident)."""
        if self._topic is None or not self.is_dirty:
            return
        new_name = self._name_buffer.strip()
        if not new_name:
            return
        topic_id = self._topic.id
        async with self._session_factory() as session:
            await update_topic(
                session,
                topic_id,
                name=new_name,
                description=self._description_buffer,
            )
            await session.commit()
        self._topic.name = new_name
        self._topic.description = self._description_buffer
        # Reseed the name buffer with the trimmed value so the field reflects what was actually
        # persisted (the user may have typed trailing whitespace).
        self._name_buffer = new_name
        self.emit(self.dirty)
        self.emit(self._saved)

    def cancel(self) -> None:
        """Discard the buffers and return to the topic's stored values."""
        if self._topic is None or not self.is_dirty:
            return
        self._name_buffer = self.original_name
        self._description_buffer = self.original_description
        self.emit(self.dirty)
