"""MultiSelectableVMMixin — selection-set state machine for VMs whose windowed list
supports a togglable "multi-select mode".

Owns a ``multi_select_active`` flag, an id-keyed ``_selected_ids`` set, the three mutators
called by ``MultiSelectableDataTable``, a ``selected_target_ids()`` resolver shared by bulk-
action mutators, and two lifecycle helpers for window churn.

Concrete-VM contract
--------------------
  - Implement ``_selectable_items()`` and ``_item_id(item)`` so the mixin can resolve the
    cursor's id without knowing the concrete attribute names.
  - Declare a ``cursor`` property (almost always already present for navigation).
  - Override ``_on_selection_changed()`` to push the new state down to sub-VMs / sibling UI
    (e.g. a details-panel freeze toggle, a linked sub-VM's target set). Default no-op.
  - Call ``_clear_selection()`` from mutators that reshuffle the window (sort / filter /
    search) — selection-by-position loses meaning after a reshuffle.
  - Call ``_intersect_selection_with_visible_ids(visible_ids)`` from a post-refetch
    ``on_complete`` callback so a bulk action doesn't leave behind ids the new window
    can't render.

Inheritance: mix in at the leaf VM, not on an intermediate base — same rule as the sibling
Searchable / Sortable mixins. The mixin's ``__init__`` is cooperative (``super().__init__()``)
and only adds two private attributes.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from rhizome.app.model import ViewModelBase


class MultiSelectableVMMixin(ViewModelBase):
    def __init__(self) -> None:
        super().__init__()
        self._multi_select_active: bool = False
        # Keyed by item id, not row index, so selection survives ``load_more``, refetches,
        # and post-action window mutations.
        self._selected_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Abstract surface — concrete VM provides
    # ------------------------------------------------------------------

    @abstractmethod
    def _selectable_items(self) -> list[Any]:
        """The current windowed list. Items are opaque to the mixin; it asks ``_item_id``
        for the key."""

    @abstractmethod
    def _item_id(self, item: Any) -> int:
        """Extract the integer id (the selection-set key) from an item."""

    @property
    @abstractmethod
    def cursor(self) -> int:
        """Row index into ``_selectable_items()``."""

    # ------------------------------------------------------------------
    # Optional hook — concrete VM overrides to sync sub-VMs / sibling UI
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """Called whenever ``multi_select_active`` flips or ``_selected_ids`` changes. The
        override reads ``self.multi_select_active`` / ``self.selected_ids`` directly."""

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def selected_ids(self) -> frozenset[int]:
        # Frozenset so accidental external mutation is impossible.
        return frozenset(self._selected_ids)

    def is_selected(self, item_id: int) -> bool:
        return item_id in self._selected_ids

    def selected_target_ids(self) -> set[int]:
        """Resolve "the selection" to a concrete set of ids. In multi-select that's
        ``_selected_ids``; in single-select it's the cursor's item id (empty if the window
        is empty). Shared by every bulk-action mutator on the concrete VM."""
        if self._multi_select_active:
            return set(self._selected_ids)
        item_id = self._cursor_item_id()
        return {item_id} if item_id is not None else set()

    # ------------------------------------------------------------------
    # Mutators — called by ``MultiSelectableDataTable``
    # ------------------------------------------------------------------

    def toggle_multi_select(self) -> None:
        """Flip multi-select mode. Turning off abandons the current selection."""
        self._multi_select_active = not self._multi_select_active
        if not self._multi_select_active:
            self._selected_ids.clear()
        self._on_selection_changed()
        self.emit(self.Callbacks.OnDirty)

    def toggle_current_selection(self) -> None:
        """Flip the cursor row's membership. No-op outside multi-select or on an empty window."""
        item_id = self._cursor_item_id()
        if item_id is None or not self._multi_select_active:
            return
        if item_id in self._selected_ids:
            self._selected_ids.discard(item_id)
        else:
            self._selected_ids.add(item_id)
        self._on_selection_changed()
        self.emit(self.Callbacks.OnDirty)

    def add_current_to_selection(self) -> None:
        """Idempotent add for ``shift+up`` / ``shift+down`` range-select sugar. Held-key
        repeat across already-selected rows is a no-op — the right behaviour for sweeping
        through an extending range."""
        item_id = self._cursor_item_id()
        if item_id is None or not self._multi_select_active:
            return
        if item_id in self._selected_ids:
            return
        self._selected_ids.add(item_id)
        self._on_selection_changed()
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Lifecycle helpers — concrete VM calls these at the right moments
    # ------------------------------------------------------------------

    def _clear_selection(self) -> None:
        """Drop the selection and notify. No-op when already empty. Use from window-
        reshuffling mutators (sort / filter / search)."""
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self._on_selection_changed()

    def _intersect_selection_with_visible_ids(self, visible: set[int]) -> None:
        """Drop selected ids no longer present in the visible window. Use from a post-
        refetch ``on_complete`` callback."""
        if not self._selected_ids:
            return
        if self._selected_ids.issubset(visible):
            return
        self._selected_ids.intersection_update(visible)
        self._on_selection_changed()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cursor_item_id(self) -> int | None:
        items = self._selectable_items()
        cursor = self.cursor
        if not items or cursor < 0 or cursor >= len(items):
            return None
        return self._item_id(items[cursor])
