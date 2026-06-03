"""``ResourceLinkerVM`` — searchable table for linking resources to the current topic.

Shaped after ``LinkedFlashcardsPanelVM``: a pinned section of resources already linked to the
topic, a boundary row, then a paginated, searchable pool of every other resource. The cursor
indexes the combined display ``[*linked, <boundary>, *remaining]``; ``cursor_section`` resolves
which region it sits in.

Linking is **staged**: toggling a row flips its membership in ``_staged_ids`` (seeded from the
linked baseline) without touching the DB. :meth:`accept` diffs the staged set against the baseline,
commits the link/unlink rows, and emits ``LINK_CHANGED`` so the loader can refetch the topic's
resources (linking changes what the loader tree shows). :meth:`cancel` reverts the staging buffer.

Like the loader, this VM mirrors the highlighted resource (``cursor_target`` + ``CURSOR_CHANGED``)
so the orchestrator can feed the preview without poking the table widget.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from rhizome.app.browser.shared.searchable import SearchableVMMixin
from rhizome.app.query_backed_vm import QueryBackedViewModel
from rhizome.db import Resource
from rhizome.db.operations import (
    count_resources,
    link_resource_to_topic,
    list_resources_for_topic,
    list_resources_paginated,
    unlink_resource_from_topic,
)

# Pool-window cap; mirrors the browser tabs. The linked section is unbounded and not paginated.
DEFAULT_PAGE_LIMIT = 500


class ResourceLinkerVM(QueryBackedViewModel, SearchableVMMixin):
    """Linker VM. Staged link/unlink over a searchable resource pool. See module docstring."""

    class Callbacks(Enum):
        # Fires after ``accept`` commits link/unlink changes — the root VM (or loader) listens to
        # refetch the topic's resources. Split from ``CURSOR_CHANGED`` (preview-feed highlight).
        LINK_CHANGED = "link_changed"
        CURSOR_CHANGED = "cursor_changed"

    def __init__(self, session_factory: Any, *, limit: int = DEFAULT_PAGE_LIMIT) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._limit = limit

        self._link_changed = self._make_group(ResourceLinkerVM.Callbacks.LINK_CHANGED)
        self._cursor_changed = self._make_group(ResourceLinkerVM.Callbacks.CURSOR_CHANGED)

        # Topic the table links against. ``None`` = no active topic.
        self._topic_id: int | None = None

        # Pinned section (resources currently linked to the topic). Frozen at fetch time.
        self._linked_resources: list[Resource] = []

        # Paginated pool of all other resources.
        self._remaining_resources: list[Resource] = []
        self._remaining_total: int | None = None
        self._remaining_has_more: bool = False

        # Active search query (empty = no filter); scopes the pool.
        self._search: str = ""

        # Cursor into the combined display ``[*linked, <boundary>, *remaining]``.
        self._cursor: int = 0

        # Staging buffer for link/unlink. Seeded from the linked baseline; toggling flips
        # membership. ``accept`` diffs against the baseline to derive link/unlink sets.
        self._staged_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def link_changed(self):
        return self._link_changed

    @property
    def cursor_changed(self):
        return self._cursor_changed

    @property
    def linked_resources(self) -> list[Resource]:
        return self._linked_resources

    @property
    def remaining_resources(self) -> list[Resource]:
        return self._remaining_resources

    @property
    def remaining_total(self) -> int | None:
        return self._remaining_total

    @property
    def remaining_has_more(self) -> bool:
        return self._remaining_has_more

    @property
    def search(self) -> str:
        return self._search

    @property
    def cursor(self) -> int:
        return self._cursor

    def is_staged_linked(self, resource_id: int) -> bool:
        return resource_id in self._staged_ids

    def _baseline_ids(self) -> set[int]:
        """The currently-linked ids — the diff baseline for ``accept``. Reseeds on every fetch."""
        return {r.id for r in self._linked_resources}

    @property
    def is_dirty_staging(self) -> bool:
        """True iff the staged set diverges from the baseline. Drives Accept/Cancel visibility."""
        return self._staged_ids != self._baseline_ids()

    def cursor_section(self) -> Literal["linked", "boundary", "remaining", "empty"]:
        n_linked = len(self._linked_resources)
        if not self._linked_resources and not self._remaining_resources:
            return "empty"
        if self._cursor < n_linked:
            return "linked"
        if self._cursor == n_linked:
            return "boundary"
        return "remaining"

    @property
    def cursor_target(self) -> Resource | None:
        """Resource under the cursor, or ``None`` on the boundary / empty display."""
        section = self.cursor_section()
        if section == "linked":
            if 0 <= self._cursor < len(self._linked_resources):
                return self._linked_resources[self._cursor]
        elif section == "remaining":
            idx = self._cursor - len(self._linked_resources) - 1  # -1 for the boundary row
            if 0 <= idx < len(self._remaining_resources):
                return self._remaining_resources[idx]
        return None

    def display_row_count(self) -> int:
        """Total rendered rows: linked + boundary + remaining (boundary always drawn)."""
        return len(self._linked_resources) + 1 + len(self._remaining_resources)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_topic(self, topic_id: int | None) -> None:
        """Point the table at a topic. Resets the cursor and refetches both sections. A ``None`` topic
        clears both sections immediately and invalidates any in-flight fetch (nothing to link against)."""
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        self._cursor = 0

        if topic_id is None:
            self._fetch_id += 1
            self._linked_resources = []
            self._remaining_resources = []
            self._remaining_total = 0
            self._remaining_has_more = False
            self._staged_ids = set()
            self._is_loading = False
            self.emit(self.dirty)
            return

        self._request_fetch()

    def set_search(self, query: str) -> None:
        new = query or ""
        if new == self._search:
            return
        self._search = new
        self._cursor = 0
        self._request_fetch()

    def set_cursor(self, index: int) -> None:
        """Move the row cursor, clamped to the display. Equality-guarded against the rebuild bounce;
        emits ``CURSOR_CHANGED`` for the preview feed."""
        total = self.display_row_count()
        new = 0 if total == 0 else max(0, min(index, total - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self.emit(self._cursor_changed)

    def toggle_current(self) -> None:
        """Flip the cursor resource's staged link membership. No-op on the boundary / empty row."""
        resource = self.cursor_target
        if resource is None:
            return
        if resource.id in self._staged_ids:
            self._staged_ids.discard(resource.id)
        else:
            self._staged_ids.add(resource.id)
        self.emit(self.dirty)

    async def load_more(self) -> None:
        """Append the next page of the pool. No-op without a topic, when a fetch is in flight, or
        when nothing more is available. ``_still_current`` gates the append against a concurrent
        supersede (a topic/search change landing mid-await)."""
        if self._topic_id is None or self._is_loading or not self._remaining_has_more:
            return
        my_id = self._fetch_id
        linked_ids = [r.id for r in self._linked_resources]
        more = await self._query_pool_window(
            exclude_ids=linked_ids, search=self._search or None, offset=len(self._remaining_resources),
        )
        if not self._still_current(my_id):
            return
        self._remaining_resources.extend(more)
        if len(more) < self._limit:
            self._remaining_has_more = False
        self.emit(self.dirty)

    async def accept(self) -> None:
        """Commit the staged diff against the topic: link the additions, unlink the removals, then emit
        ``LINK_CHANGED`` (so the loader refetches the topic's resources) and refetch our own sections,
        which rebases the linked baseline and clears the staging."""
        if not self.is_dirty_staging or self._topic_id is None:
            return
        baseline = self._baseline_ids()
        to_link = self._staged_ids - baseline
        to_unlink = baseline - self._staged_ids
        async with self._session_factory() as session:
            for rid in to_link:
                await link_resource_to_topic(session, resource_id=rid, topic_id=self._topic_id)
            for rid in to_unlink:
                await unlink_resource_from_topic(session, resource_id=rid, topic_id=self._topic_id)
            await session.commit()
        self.emit(self._link_changed)
        self._request_fetch()

    def cancel(self) -> None:
        """Revert the staging buffer to the linked baseline."""
        if not self.is_dirty_staging:
            return
        self._staged_ids = self._baseline_ids()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # QueryBackedViewModel contract
    # ------------------------------------------------------------------

    async def _query_pool_window(
        self, *, exclude_ids: list[int], search: str | None, offset: int,
    ) -> list[Resource]:
        async with self._session_factory() as session:
            return await list_resources_paginated(
                session, exclude_ids=exclude_ids, search=search, limit=self._limit, offset=offset,
            )

    async def _fetch(self) -> tuple[list[Resource], list[Resource], int]:
        # Snapshot the inputs, then run the linked query (its ids are the pool's ``exclude_ids``) and
        # the pool window + count. Search scopes the pool only — the linked section is unconditional.
        topic_id = self._topic_id
        search = self._search or None
        if topic_id is None:
            return [], [], 0
        async with self._session_factory() as session:
            linked = await list_resources_for_topic(session, topic_id)
            linked_ids = [r.id for r in linked]
            remaining = await list_resources_paginated(
                session, exclude_ids=linked_ids, search=search, limit=self._limit, offset=0,
            )
            total = await count_resources(session, exclude_ids=linked_ids, search=search)
        return linked, remaining, total

    def _process_fetched_data(
        self, result: tuple[list[Resource], list[Resource], int],
    ) -> None:
        linked, remaining, remaining_total = result

        # Reseed staging only when the linked baseline actually changed (topic switch, or our own
        # accept just committed). A search-driven refetch leaves the baseline untouched, so in-progress
        # staging is preserved — the user can search, stage, search again, and accumulate before accept.
        old_baseline = {r.id for r in self._linked_resources}
        new_baseline = {r.id for r in linked}

        self._linked_resources = linked
        self._remaining_resources = remaining
        self._remaining_total = remaining_total
        self._remaining_has_more = len(remaining) < remaining_total

        if new_baseline != old_baseline:
            self._staged_ids = set(new_baseline)

        total = self.display_row_count()
        if self._cursor >= total:
            self._cursor = max(0, total - 1)
