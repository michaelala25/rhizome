"""Topic-tree VM. Holds the selection set, cursor topic id, and the DB-facing reads (children
fetch, subtree delete, create-topic). The TopicTree view (in ``rhizome.tui.widgets.browser.topics.tree``)
owns the visual tree structure.

Selection is **cascade-on-toggle**: toggling a topic expands its subtree via the recursive CTE and
either adds or removes the whole subtree based on full-coverage. The consequence is that
``_selected_ids`` *is* the expanded filter set, so ``expanded_filter_ids()`` is a sync read with no
second-stage CTE at filter-propagation time. Partial coverage (cascade-add then explicitly uncheck
a descendant) counts as not-fully-selected, so a re-toggle re-adds the whole subtree — standard
tri-state file-picker behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rhizome.app.model import ViewModelBase
from rhizome.db import Topic
from rhizome.db.operations import (
    create_topic,
    delete_topic_subtree,
    expand_subtrees,
    find_parent_topic_ids,
    list_children,
    list_root_topics,
)
from rhizome.logs import get_logger

_logger = get_logger("browser.topic_tree")


@dataclass(frozen=True)
class LoadedTopic:
    """A topic plus a precomputed ``has_children`` hint, returned by ``fetch_children``. The hint
    comes from a single batched ``find_parent_topic_ids`` against the peer cohort, sparing the view
    a follow-up query when it builds each ``TreeNode``."""
    topic: Topic
    has_children: bool


class TopicTreeModel(ViewModelBase):
    """Multi-select topic tree VM. Holds selection + cursor id + DB reads; the view holds the rest."""

    class Callbacks(ViewModelBase.Callbacks):
        # No payloads — listeners read public accessors. ``OnCursorChanged`` is split from
        # ``OnDirty`` so consumers like the topic-summary panel don't refetch on every selection-
        # toggle repaint. ``OnTopicDeleted`` fires after a subtree delete commits; the browser
        # orchestrator listens so the active tab can drop rows whose topic just vanished.
        OnSelectionChanged = "OnSelectionChanged"
        OnCursorChanged    = "OnCursorChanged"
        OnTopicDeleted     = "OnTopicDeleted"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self.make_callback_groups({
            self.Callbacks.OnSelectionChanged: None,
            self.Callbacks.OnCursorChanged:    None,
            self.Callbacks.OnTopicDeleted:     None,
        })
        self._selected_ids: set[int] = set()
        # Authoritative external reference; mirrors the widget's own cursor whenever the view pushes
        # a ``set_cursor``. Other code reads it without poking the widget.
        self._cursor_topic_id: int | None = None

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    def is_selected(self, topic_id: int) -> bool:
        return topic_id in self._selected_ids

    @property
    def selected_ids(self) -> frozenset[int]:
        return frozenset(self._selected_ids)

    @property
    def cursor_topic_id(self) -> int | None:
        return self._cursor_topic_id

    # ------------------------------------------------------------------
    # DB-facing operations
    # ------------------------------------------------------------------

    async def fetch_children(self, parent_id: int | None) -> list[LoadedTopic]:
        """Direct children of ``parent_id`` (or the roots when ``None``), each with a ``has_children``
        hint from a batched ``find_parent_topic_ids``. Stateless — the view holds the result inside
        ``TreeNode``s rather than the VM caching it."""
        async with self._session_factory() as session:
            if parent_id is None:
                topics = await list_root_topics(session)
            else:
                topics = await list_children(session, parent_id)
            parent_set = await find_parent_topic_ids(session, [t.id for t in topics])
        return [LoadedTopic(topic=t, has_children=t.id in parent_set) for t in topics]

    async def delete_topic_subtree(self, root_id: int) -> set[int]:
        """Delete ``root_id`` and its full subtree (FK cascade handles entries / flashcards).
        Drops any selected ids that just vanished and clears the cursor if it pointed into the
        deleted subtree. Emits ``Callbacks.OnTopicDeleted`` so the browser orchestrator can refetch
        the active tab. Returns the set of deleted topic ids."""
        async with self._session_factory() as session:
            deleted_ids = await delete_topic_subtree(session, root_id)
            await session.commit()
        if deleted_ids & self._selected_ids:
            self._selected_ids -= deleted_ids
            self.emit(self.Callbacks.OnSelectionChanged)
        if self._cursor_topic_id in deleted_ids:
            self._cursor_topic_id = None
            self.emit(self.Callbacks.OnCursorChanged)
        self.emit(self.Callbacks.OnDirty)
        self.emit(self.Callbacks.OnTopicDeleted)
        return deleted_ids

    async def create_topic(self, parent_id: int | None) -> Topic:
        """Create a new topic under ``parent_id`` (``None`` = root) and return it. The name is
        auto-generated as ``"Untitled Topic <id>"`` — we flush to get the id, then mutate the
        in-memory row's name in place before committing so the column never holds the placeholder."""
        async with self._session_factory() as session:
            topic = await create_topic(session, name="Untitled Topic", parent_id=parent_id)
            topic.name = f"Untitled Topic {topic.id}"
            await session.commit()
        return topic

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    async def toggle_selection(self, topic_id: int) -> None:
        """Toggle ``topic_id`` with subtree cascade — expand once via the CTE, then add the whole
        subtree if any descendant was missing or remove the whole subtree if it was fully covered.
        Emits ``OnDirty`` + ``OnSelectionChanged`` exactly once even when the cascade moves many ids."""
        async with self._session_factory() as session:
            subtree = await expand_subtrees(session, [topic_id])
        if subtree.issubset(self._selected_ids):
            self._selected_ids.difference_update(subtree)
        else:
            self._selected_ids.update(subtree)
        self.emit(self.Callbacks.OnDirty)
        self.emit(self.Callbacks.OnSelectionChanged)

    def clear_selection(self) -> None:
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self.emit(self.Callbacks.OnDirty)
        self.emit(self.Callbacks.OnSelectionChanged)

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def set_cursor(self, topic_id: int | None) -> None:
        if self._cursor_topic_id == topic_id:
            return
        self._cursor_topic_id = topic_id
        self.emit(self.Callbacks.OnDirty)
        self.emit(self.Callbacks.OnCursorChanged)

    # ------------------------------------------------------------------
    # Filter projection
    # ------------------------------------------------------------------

    def expanded_filter_ids(self) -> frozenset[int] | None:
        """``None`` for empty selection (no filter); otherwise the selection set as a frozenset.
        Sync read — cascade-on-toggle has already done the subtree expansion."""
        if not self._selected_ids:
            return None
        return frozenset(self._selected_ids)
