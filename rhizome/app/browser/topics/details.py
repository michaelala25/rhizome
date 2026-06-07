"""Buffered-edit VM for the topic details panel (under the topic tree in the browser's left rail).

Editing model: each field has an in-VM buffer seeded from the topic on every fetch. When either
buffer diverges from the loaded topic the VM is ``is_dirty`` and the view reveals an Accept/Cancel
row. Accept persists via ``update_topic`` + commit and mutates the in-memory ``Topic`` in place;
Cancel restores from the topic.

Cursor-move-while-dirty: silent discard. The panel VM calls ``set_topic_id`` on every cursor move
and buffers are unconditionally reseeded once the new topic resolves. While the fetch is in flight
buffers stay on the prior topic — short-lived because ``Query`` collapses bursts in its debounce.

Callback groups:
  * ``Callbacks.OnDirty`` — standard repaint signal.
  * ``Callbacks.OnSaved`` — fires only after a successful Accept. The panel view subscribes so it
    can repaint the tree node's label after a rename.
"""

from __future__ import annotations

from dataclasses import dataclass
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

from rhizome.app.model import ViewModelBase
from rhizome.app.query import Query, QueryState

_logger = get_logger("browser.topic_details")


@dataclass(frozen=True)
class LoadedTopicDetails:
    """Output of one fetch: the topic plus the direct/subtree counts that render below the
    description. Bundled so a single fetch carries everything the view needs."""
    topic: Topic | None
    direct_entries: int
    subtree_entries: int
    direct_flashcards: int
    subtree_flashcards: int


_EMPTY_DETAILS = LoadedTopicDetails(None, 0, 0, 0, 0)


class TopicDetailsModel(ViewModelBase):
    """Buffered-edit VM for the topic details panel. Accept/Cancel are the explicit exits from dirty;
    nothing reaches the DB until the user Accepts."""

    class Callbacks(ViewModelBase.Callbacks):
        OnSaved = "OnSaved"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self.make_callback_groups({self.Callbacks.OnSaved: None})

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

        # Single-query driver. Cached by topic_id so re-navigating to a previously-viewed topic
        # restores the details synchronously; ``None`` is a legal cache key (the empty case).
        self._query: Query[int | None, LoadedTopicDetails] = Query(
            fetch=self._fetch,
            cache_key=lambda p: p,
            on_change=self._on_query_change,
        )

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

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
        """Switch the panel to ``topic_id``. Idempotent on the same id. ``None`` submits the same
        query path — ``_fetch`` short-circuits to an empty result, which clears the panel after the
        debounce tick."""
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        self._query.submit(topic_id)

    # ------------------------------------------------------------------
    # Query driver
    # ------------------------------------------------------------------

    async def _fetch(self, topic_id: int | None) -> LoadedTopicDetails:
        if topic_id is None:
            return _EMPTY_DETAILS
        async with self._session_factory() as session:
            topic = await get_topic(session, topic_id)
            if topic is None:
                return _EMPTY_DETAILS
            subtree_ids = await expand_subtrees(session, [topic_id])
            direct_entries = await count_entries(session, topic_id)
            subtree_entries = await count_entries_filtered(session, topic_ids=subtree_ids)
            direct_flashcards = await count_flashcards_by_topic(session, topic_id)
            subtree_flashcards = await count_flashcards_by_topics(session, subtree_ids)
        return LoadedTopicDetails(
            topic=topic,
            direct_entries=direct_entries,
            subtree_entries=subtree_entries,
            direct_flashcards=direct_flashcards,
            subtree_flashcards=subtree_flashcards,
        )

    def _on_query_change(self) -> None:
        # State transitions to READY are the only ones that carry new data; LOADING / SLOW / ERROR
        # still emit a repaint so loading affordances (if any) can refresh.
        if self._query.state is QueryState.READY and self._query.result is not None:
            self._apply_fetched_data(self._query.result)
        self.emit(self.Callbacks.OnDirty)

    def _apply_fetched_data(self, result: LoadedTopicDetails) -> None:
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
        self.emit(self.Callbacks.OnDirty)

    def set_description(self, value: str) -> None:
        if self._topic is None:
            return
        if value == self._description_buffer:
            return
        self._description_buffer = value
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Accept / Cancel
    # ------------------------------------------------------------------

    async def accept(self) -> None:
        """Persist the buffers and mutate the in-memory topic in place. Emits ``OnSaved`` so the
        panel view can repaint the tree node label. No-ops on empty name (the DB column is
        non-null and an empty rename is almost always an accident).

        Invalidates the topic's cache entry so a subsequent ``set_topic_id`` re-fetch sees the
        updated values rather than a stale cached snapshot."""
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
        self._query.invalidate(predicate=lambda k: k == topic_id)
        self.emit(self.Callbacks.OnDirty)
        self.emit(self.Callbacks.OnSaved)

    def cancel(self) -> None:
        """Discard the buffers and return to the topic's stored values."""
        if self._topic is None or not self.is_dirty:
            return
        self._name_buffer = self.original_name
        self._description_buffer = self.original_description
        self.emit(self.Callbacks.OnDirty)
