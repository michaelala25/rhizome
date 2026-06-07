"""Knowledge-entry tab VM. Windowed list of ``KnowledgeEntry`` plus the bulk-action surface.

Owns data facts only — the loaded window, search / sort / filter / cursor / selection state, and the
child detail + linked-flashcards sub-VMs. Dialog UI state (cursor, which dialog is open) lives view-
side. Search / sort / filter mutators discard the window and refetch from offset 0; ``load_more``
appends in place without touching the cursor.

Single ``PagedQuery`` keyed on the param tuple ``(topic_ids, search, entry_types, has_flashcards,
flashcard_ids, sort_by, sort_dir)``. Caching is off (``cache_key=None``): every distinct param set
re-fetches. Cache could be added later, but it would need invalidation on every DB mutation that
touches entries (delete, change_topic, change_type), so the simpler default is "no cache."

Selection-on-refetch contract: ``_on_query_change`` always runs
``_intersect_selection_with_visible_ids`` on a READY transition. Every filter-axis mutator clears
the selection first, so the intersect is a no-op for those; the bulk-action refetch path
(``_post_change_refetch``) deliberately preserves the selection and lets the intersect prune ids
that fell out of the new window.
"""

from __future__ import annotations

import enum
from typing import Any, Iterable, Literal

from rhizome.db import KnowledgeEntry
from rhizome.db.models import EntryType
from rhizome.db.operations import (
    EntrySortKey,
    count_entries_filtered,
    delete_entry,
    list_entries_paginated,
    update_entry,
)
from rhizome.logs import get_logger

from rhizome.app.browser.shared.searchable import SearchableModelMixin
from rhizome.app.browser.shared.multiselectable import MultiSelectableVMMixin
from rhizome.app.browser.shared.sortable import SortableVMMixin
from rhizome.app.browser.tab_base import BrowserTabModel
from rhizome.app.query import PagedQuery, QueryState
from .entry_details import EntryDetailsModel
from .linked_flashcards import LinkedFlashcardsPanelModel

_logger = get_logger("browser.knowledge_entry_tab")

# Hard cap on rows fetched per page. Bounded memory/render at 100K+ entries; "showing 500 of N+,
# load more" is the simplest UX that scales. See ``braindump.md`` for the long-form rationale.
DEFAULT_PAGE_LIMIT = 500

# Param tuple for the entries query. All axes are hashable so the tuple itself is hashable —
# important if caching gets turned on later.
EntryParams = tuple[
    frozenset[int] | None,           # topic_ids (filter)
    str,                             # search   (empty string = no filter)
    tuple[EntryType, ...] | None,    # entry_types
    bool | None,                     # has_flashcards
    tuple[int, ...] | None,          # flashcard_ids
    EntrySortKey,                    # sort_by
    Literal["asc", "desc"],          # sort_dir
]


class EntryTabModel(
    BrowserTabModel,
    SearchableModelMixin,
    SortableVMMixin["EntrySortKey"],
    MultiSelectableVMMixin,
):
    """Concrete tab VM for browsing knowledge entries."""

    TITLE = "Knowledge Entries"

    class State(enum.Enum):
        """Right-pane layout state. ``transition_to`` is the only mutator; the view branches its
        layout off ``vm.state``."""

        ENTRIES = "entries"
        LINKED_FLASHCARDS = "linked_flashcards"

    def __init__(
        self,
        session_factory: Any,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._limit = limit

        self._state: State = self.State.ENTRIES

        # Topic filter (pushed in by the orchestrator). ``_filter_applied`` distinguishes "filter
        # is None by default" from "filter has never been set" — the first call must fetch even
        # when the requested filter happens to equal the default.
        self._filter_ids: frozenset[int] | None = None
        self._filter_applied: bool = False

        # Search / sort / filter state. ``None`` = no filter; an empty tuple is a legal "no rows
        # match" terminal state. ``_has_flashcards`` and ``_flashcard_ids`` are a tagged union —
        # see ``set_flashcard_filter``.
        self._search: str = ""
        self._sort_by: EntrySortKey = "id"
        self._sort_dir: Literal["asc", "desc"] = "asc"
        self._entry_types: tuple[EntryType, ...] | None = None
        self._has_flashcards: bool | None = None
        self._flashcard_ids: tuple[int, ...] | None = None

        # Window-local row cursor (not an entry id — survives ``load_more``).
        self._cursor: int = 0

        # Multi-select state (flag, id-keyed selection set, mutators, ``selected_target_ids`` etc.)
        # lives on the mixin. We override ``toggle_multi_select`` to drop relink mode on entry.

        # Detail panel sub-VM. Subscribe to its SAVED group so we can repaint the DataTable row
        # after an Accept (the in-memory ``KnowledgeEntry`` was mutated in place).
        self._details = EntryDetailsModel(session_factory)
        self._details.subscribe(self._details.Callbacks.OnSaved, self._on_details_saved)

        # Linked-flashcards sub-VM. Fed via ``_sync_linked_flashcards``, which is state-gated so it
        # doesn't fire fetches outside ``LINKED_FLASHCARDS``.
        self._linked_flashcards = LinkedFlashcardsPanelModel(session_factory)

        # The entries window. Cache off (see module docstring).
        self._query: PagedQuery[EntryParams, KnowledgeEntry] = PagedQuery(
            fetch_page=self._fetch_page,
            count=self._fetch_count,
            page_size=limit,
            on_change=self._on_query_change,
        )

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------
    #
    # ``title`` comes from ``BrowserTabModel``.

    @property
    def state(self) -> State:
        return self._state

    @property
    def entries(self) -> list[KnowledgeEntry]:
        return self._query.current.rows if self._query.current is not None else []

    @property
    def total(self) -> int | None:
        return self._query.current.total if self._query.current is not None else None

    @property
    def has_more(self) -> bool:
        return self._query.current is not None and self._query.current.has_more

    @property
    def is_loading(self) -> bool:
        return self._query.state in {QueryState.LOADING, QueryState.SLOW}

    @property
    def filter_ids(self) -> frozenset[int] | None:
        return self._filter_ids

    @property
    def search(self) -> str:
        return self._search

    @property
    def sort_by(self) -> EntrySortKey:
        return self._sort_by

    @property
    def sort_dir(self) -> Literal["asc", "desc"]:
        return self._sort_dir

    # Sort axes surfaced in the ``SortMenu``, ordered to match the table's column order. First
    # entry doubles as the dialog's reset target.
    _SORT_OPTIONS: tuple[EntrySortKey, ...] = ("id", "title", "type", "topic")

    def sort_options(self) -> tuple[EntrySortKey, ...]:
        return self._SORT_OPTIONS

    @property
    def entry_types(self) -> tuple[EntryType, ...] | None:
        return self._entry_types

    @property
    def has_flashcards(self) -> bool | None:
        return self._has_flashcards

    @property
    def flashcard_ids(self) -> tuple[int, ...] | None:
        return self._flashcard_ids

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def details(self) -> EntryDetailsModel:
        return self._details

    @property
    def linked_flashcards(self) -> LinkedFlashcardsPanelModel:
        return self._linked_flashcards

    @property
    def session_factory(self) -> Any:
        # Exposed so the view can hand the same factory to modal screens (e.g. ``TopicSelectorScreen``)
        # without reaching into the private attr.
        return self._session_factory

    # ------------------------------------------------------------------
    # Multi-select abstract surface
    # ------------------------------------------------------------------

    def _selectable_items(self) -> list[KnowledgeEntry]:
        return self.entries

    def _item_id(self, item: KnowledgeEntry) -> int:
        return item.id

    def _on_selection_changed(self) -> None:
        # Push selection state down to both sub-VMs whenever the flag flips or the set changes.
        self._details.set_multi_select(
            self._multi_select_active,
            len(self._selected_ids),
        )
        self._sync_linked_flashcards()

    # ------------------------------------------------------------------
    # Param snapshot + fetch impls
    # ------------------------------------------------------------------

    def _params(self) -> EntryParams:
        return (
            self._filter_ids,
            self._search,
            self._entry_types,
            self._has_flashcards,
            self._flashcard_ids,
            self._sort_by,
            self._sort_dir,
        )

    async def _fetch_page(
        self, params: EntryParams, offset: int, limit: int,
    ) -> list[KnowledgeEntry]:
        topic_ids, search, entry_types, has_flashcards, flashcard_ids, sort_by, sort_dir = params
        async with self._session_factory() as session:
            return await list_entries_paginated(
                session,
                limit=limit,
                offset=offset,
                topic_ids=topic_ids,
                search=search or None,
                entry_types=list(entry_types) if entry_types is not None else None,
                has_flashcards=has_flashcards,
                flashcard_ids=list(flashcard_ids) if flashcard_ids is not None else None,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )

    async def _fetch_count(self, params: EntryParams) -> int:
        topic_ids, search, entry_types, has_flashcards, flashcard_ids, _sort_by, _sort_dir = params
        async with self._session_factory() as session:
            return await count_entries_filtered(
                session,
                topic_ids=topic_ids,
                search=search or None,
                entry_types=list(entry_types) if entry_types is not None else None,
                has_flashcards=has_flashcards,
                flashcard_ids=list(flashcard_ids) if flashcard_ids is not None else None,
            )

    def _on_query_change(self) -> None:
        # Every READY transition (whether from a filter mutator or a bulk-action refetch) runs the
        # selection intersect. Filter mutators ``_clear_selection()`` first so the intersect is a
        # no-op for those; bulk-action paths deliberately keep the selection so the intersect
        # prunes ids that fell out of the new window.
        if self._query.state is QueryState.READY and self._query.current is not None:
            self._clamp_cursor()
            self._sync_details()
            self._sync_linked_flashcards()
            self._intersect_selection_with_visible_ids({e.id for e in self.entries})
        self.emit(self.Callbacks.OnDirty)

    def _clamp_cursor(self) -> None:
        rows = self.entries
        if not rows:
            self._cursor = 0
        elif self._cursor >= len(rows):
            self._cursor = len(rows) - 1

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def transition_to(self, new_state: State) -> None:
        """Idempotent layout transition. Silently discards any unsaved title/content edits in the
        details panel (matches the silent-discard policy on cursor-move-while-dirty). Multi-select
        state is preserved (the set is entry-id-keyed). Dialog dismissal on state change is the
        view's concern."""
        if new_state is self._state:
            return
        _logger.info("Tab state transition: %s -> %s", self._state.value, new_state.value)

        self._details.cancel()

        self._state = new_state

        # ``_sync_linked_flashcards`` is state-gated. On exit to ENTRIES we explicitly push the empty
        # set to invalidate any in-flight fetch + free the loaded window, and exit relink (the panel
        # is no longer visible).
        if new_state is self.State.LINKED_FLASHCARDS:
            self._sync_linked_flashcards()
        else:
            self._linked_flashcards.exit_relink_mode()
            self._linked_flashcards.set_entry_ids(frozenset())

        self.emit(self.Callbacks.OnDirty)

    def set_topic_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Set the active topic filter and (re)fetch if it actually changed. Idempotent on equal
        filters — needed so the orchestrator's lazy tab catch-up doesn't paint a loading flash when
        switching to a tab that already matches the current filter."""
        # Push to the panel first so the sub-VM sees the new filter at the same logical moment we
        # do, before our own refetch could land. Order doesn't matter for correctness (both ends
        # fetch-id-reconcile), just avoids a brief stale-filter pool query.
        self._linked_flashcards.set_topic_filter(topic_ids)

        new_filter: frozenset[int] | None = None if topic_ids is None else frozenset(topic_ids)
        if self._filter_applied and new_filter == self._filter_ids:
            return
        self._filter_ids = new_filter
        self._filter_applied = True
        self._query.set_params(self._params())

    def refetch(self) -> None:
        """Re-run the current query without changing inputs. Used by the orchestrator after
        out-of-band data changes (e.g. a topic rename) that may have invalidated cached rows."""
        self._query.set_params(self._params())

    def set_search(self, query: str) -> None:
        new = query or ""
        if new == self._search:
            return
        self._search = new
        self._cursor = 0
        self._clear_selection()
        self._query.set_params(self._params())

    def set_sort(
        self,
        sort_by: EntrySortKey,
        sort_dir: Literal["asc", "desc"] = "asc",
    ) -> None:
        if sort_by == self._sort_by and sort_dir == self._sort_dir:
            return
        self._sort_by = sort_by
        self._sort_dir = sort_dir
        self._cursor = 0
        self._clear_selection()
        self._query.set_params(self._params())

    def set_type_filter(self, entry_types: tuple[EntryType, ...] | None) -> None:
        new = None if entry_types is None else tuple(entry_types)
        if new == self._entry_types:
            return
        self._entry_types = new
        self._cursor = 0
        self._clear_selection()
        self._query.set_params(self._params())

    def set_flashcard_filter(self, has_flashcards: bool | None) -> None:
        """Set the boolean axis of the flashcard filter. Wipes ``_flashcard_ids`` — the dialog
        presents the two axes as one tagged radio, so the VM enforces mutual exclusion."""
        if has_flashcards == self._has_flashcards and self._flashcard_ids is None:
            return
        self._has_flashcards = has_flashcards
        self._flashcard_ids = None
        self._cursor = 0
        self._clear_selection()
        self._query.set_params(self._params())

    def set_flashcard_ids_filter(self, flashcard_ids: tuple[int, ...] | None) -> None:
        """Set the "one of these flashcards" axis. Wipes ``_has_flashcards`` (see
        ``set_flashcard_filter``). Empty tuple is a legal "no rows match" terminal state."""
        new = None if flashcard_ids is None else tuple(flashcard_ids)
        if new == self._flashcard_ids and self._has_flashcards is None:
            return
        self._flashcard_ids = new
        self._has_flashcards = None
        self._cursor = 0
        self._clear_selection()
        self._query.set_params(self._params())

    def set_cursor(self, index: int) -> None:
        """Move the row cursor. Clamped to the loaded window.

        Deliberately does **not** emit ``dirty`` — a tab-view ``_refresh`` rebuilds the DataTable,
        which fires ``RowHighlighted`` and round-trips back here. The equality guard kills the
        bounce in one trip rather than letting it loop. Cursor moves remain visible via the table's
        own render and the detail panel's separate ``dirty``."""
        rows = self.entries
        if not rows:
            new = 0
        else:
            new = max(0, min(index, len(rows) - 1))
        if new == self._cursor:
            return
        self._cursor = new
        self._sync_details()
        self._sync_linked_flashcards()
        self.emit(self.Callbacks.OnDirty)

    def toggle_multi_select(self) -> None:
        # Drop relink first — relink is single-select only, so its precondition vanishes the
        # moment multi-select turns on. Mixin owns the flag flip + clear + ``_on_selection_changed``
        # + ``dirty`` emit.
        if not self._multi_select_active:
            self._linked_flashcards.exit_relink_mode()
        super().toggle_multi_select()

    def enter_relink_mode(self) -> None:
        """Combined-motion entry to relink: drop multi-select, transition to LINKED_FLASHCARDS,
        turn on relink. One-directional — pair with ``exit_relink_mode`` for the toggle-off."""
        if self._multi_select_active:
            # Inline the multi-select exit (rather than ``toggle_multi_select``) so we get one final
            # ``dirty`` emit at the bottom of this method instead of two. The mixin's
            # ``_on_selection_changed`` hook still does the sub-VM propagation.
            self._multi_select_active = False
            self._selected_ids.clear()
            self._on_selection_changed()
        if self._state is not self.State.LINKED_FLASHCARDS:
            self.transition_to(self.State.LINKED_FLASHCARDS)
        self._linked_flashcards.enter_relink_mode()
        self.emit(self.Callbacks.OnDirty)

    def exit_relink_mode(self) -> None:
        # Stays in LINKED_FLASHCARDS — only ``transition_to(ENTRIES)`` walks all the way out.
        self._linked_flashcards.exit_relink_mode()
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Bulk actions on the selection
    # ------------------------------------------------------------------
    #
    # "The selection" is ``selected_target_ids()``: live ``_selected_ids`` in multi-select; the
    # cursor entry id in single-select. All three actions no-op against an empty result.

    async def delete_selected_entries(self) -> None:
        """Delete target entries in a single session + commit; FK cascade cleans link rows in
        ``flashcard_entry``. Prunes the loaded window / decrements ``total`` / clamps the cursor
        in place — no refetch. Multi-select stays on so the visual context is preserved."""
        targets = self.selected_target_ids()
        if not targets:
            return

        async with self._session_factory() as session:
            for entry_id in targets:
                await delete_entry(session, entry_id)
            await session.commit()
        _logger.info("Deleted %d entries", len(targets))

        # Mutate the loaded window in place. The PagedList is the live transport — no separate
        # ``_entries`` copy to keep in sync.
        if self._query.current is not None:
            self._query.current.rows = [
                e for e in self._query.current.rows if e.id not in targets
            ]
            if self._query.current.total is not None:
                self._query.current.total = max(0, self._query.current.total - len(targets))

        if self._multi_select_active:
            self._selected_ids.clear()
        self._clamp_cursor()

        self._sync_details()
        self._sync_linked_flashcards()
        self._details.set_multi_select(self._multi_select_active, 0)
        self.emit(self.Callbacks.OnDirty)

    async def change_topic_on_selected_entries(self, new_topic_id: int) -> None:
        """Reassign target entries' topic + refetch (rather than in-place mutate) so any active
        topic filter re-evaluates. Selection is preserved across the refetch only for entries
        still in the new window — see the module docstring."""
        targets = self.selected_target_ids()
        if not targets:
            return
        async with self._session_factory() as session:
            for entry_id in targets:
                await update_entry(session, entry_id, topic_id=new_topic_id)
            await session.commit()
        _logger.info("Re-topicked %d entries to topic %d", len(targets), new_topic_id)
        self._post_change_refetch()

    async def change_type_on_selected_entries(self, new_type: EntryType) -> None:
        """Reassign target entries' type + refetch. Same selection-preservation rule as
        ``change_topic_on_selected_entries``."""
        targets = self.selected_target_ids()
        if not targets:
            return
        async with self._session_factory() as session:
            for entry_id in targets:
                await update_entry(session, entry_id, entry_type=new_type)
            await session.commit()
        _logger.info("Retyped %d entries to %s", len(targets), new_type.value)
        self._post_change_refetch()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    #
    # ``selected_target_ids`` / ``_clear_selection`` / ``_intersect_selection_with_visible_ids``
    # come from ``MultiSelectableVMMixin``.

    def _post_change_refetch(self) -> None:
        # Bulk-action refetch path: deliberately does NOT call ``_clear_selection()``. The selection
        # survives the refetch and is then pruned to entries still in the new window by the
        # intersect inside ``_on_query_change``. See the module docstring.
        self._query.set_params(self._params())

    def _sync_details(self) -> None:
        """Push the cursor entry (or ``None``) into the detail sub-VM. Called from ``set_cursor``
        and ``_on_query_change``."""
        rows = self.entries
        if not rows or self._cursor >= len(rows):
            self._details.set_entry(None)
            return
        self._details.set_entry(rows[self._cursor])

    def _sync_linked_flashcards(self) -> None:
        """Push the current entry-id target set into the linked-flashcards sub-VM. State-gated:
        skipped outside LINKED_FLASHCARDS to avoid spurious fetches. ``set_entry_ids`` is
        idempotent, so chatter under cursor moves in multi-select is free."""
        if self._state is not self.State.LINKED_FLASHCARDS:
            return
        self._linked_flashcards.set_entry_ids(self._linked_flashcards_target_ids())

    def _linked_flashcards_target_ids(self) -> frozenset[int]:
        """Multi-select: the live selection set (may be empty, which is a legal display state).
        Single-select: the cursor entry id (or empty if the window is empty). Distinct from
        ``selected_target_ids`` because that one always falls back to the cursor entry."""
        rows = self.entries
        if self._multi_select_active:
            return frozenset(self._selected_ids)
        if not rows or self._cursor >= len(rows):
            return frozenset()
        return frozenset({rows[self._cursor].id})

    def _on_details_saved(self) -> None:
        # Details panel mutated the in-memory ``KnowledgeEntry`` in place; just kick a repaint so
        # the DataTable row picks up the new values.
        self.emit(self.Callbacks.OnDirty)

    async def load_more(self) -> None:
        """Append the next page in place. No cursor move. ``PagedQuery.load_more`` handles the
        re-entry guard and gates the append on its own staleness check so a concurrent supersede
        doesn't extend the new window with stale tail rows."""
        await self._query.load_more()
