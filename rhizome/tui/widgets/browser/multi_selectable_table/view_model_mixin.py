"""MultiSelectableViewModelMixin — selection-set state machine for VMs whose windowed list
supports a "multi-select mode" the user can toggle on/off.

The mixin owns the small, well-bounded state machine: a ``multi_select_active`` flag, an id-
keyed ``_selected_ids`` set, and the three mutators (``toggle_multi_select``,
``toggle_current_selection``, ``add_current_to_selection``) plus the supporting
``selected_target_ids`` resolver and the window-shrink helpers
(``_clear_selection`` / ``_intersect_selection_with_visible_ids``). Concrete VMs:

  - Implement ``_selectable_items()`` and ``_item_id(item)`` so the mixin can find the
    cursor's id without knowing the concrete attribute names.
  - Declare a ``cursor`` property (almost always already present).
  - Override ``_on_selection_changed()`` to push the new state down to sub-VMs / sibling UI
    (e.g. a details panel's freeze toggle, a linked-flashcards sub-VM's target set).
  - Call ``_clear_selection()`` from mutators that reshuffle the window
    (sort / filter / search changes).
  - Call ``_intersect_selection_with_visible_ids(visible_ids)`` from a post-refetch
    ``on_complete`` callback to drop any ids that no longer survive the new window.

Inheritance convention
----------------------
Mix in at the leaf, not on an intermediate base — same rule as
``SearchableViewModelMixin`` / ``SortableViewModelMixin``. The mixin's ``__init__`` runs
cooperatively via ``super().__init__()`` and only adds two private attributes, so it slots
into any existing MRO that ends at ``ViewModelBase`` without surprises.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from ...view_model_base import ViewModelBase


class MultiSelectableViewModelMixin(ViewModelBase):
    def __init__(self) -> None:
        super().__init__()
        self._multi_select_active: bool = False
        # Keyed by item id, not row index, so the selection survives ``load_more``, refetches,
        # and post-action window mutations. Empty in single-select; never grows then either.
        self._selected_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Abstract surface — concrete VM provides
    # ------------------------------------------------------------------

    @abstractmethod
    def _selectable_items(self) -> list[Any]:
        """The current windowed list. The mixin only reads via index; the items themselves
        are opaque (the mixin asks ``_item_id`` for the key)."""

    @abstractmethod
    def _item_id(self, item: Any) -> int:
        """Extract the integer id (the selection-set key) from an item."""

    @property
    @abstractmethod
    def cursor(self) -> int:
        """Current row index into ``_selectable_items()``. Concrete VMs almost always already
        own this for navigation purposes; the mixin reads it to locate the cursor's id."""

    # ------------------------------------------------------------------
    # Optional hook — concrete VM overrides to thread state down to sub-VMs / sibling UI
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """Called whenever ``multi_select_active`` flips or ``_selected_ids`` changes. The
        hook is argless; the concrete override reads ``self.multi_select_active`` /
        ``self.selected_ids`` directly. Default no-op."""

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def multi_select_active(self) -> bool:
        return self._multi_select_active

    @property
    def selected_ids(self) -> frozenset[int]:
        """Read-only view of the current selection set. Frozenset rather than the raw mutable
        set so accidental external mutation is impossible."""
        return frozenset(self._selected_ids)

    def is_selected(self, item_id: int) -> bool:
        return item_id in self._selected_ids

    def selected_target_ids(self) -> set[int]:
        """Resolve "the selection" to a concrete set of ids. In multi-select that's
        ``_selected_ids``; in single-select it's the cursor's item id (empty if the window
        is empty). Shared by every bulk-action mutator the concrete VM writes."""
        if self._multi_select_active:
            return set(self._selected_ids)
        item_id = self._cursor_item_id()
        return {item_id} if item_id is not None else set()

    # ------------------------------------------------------------------
    # Mutators — called by the ``MultiSelectableDataTable`` widget
    # ------------------------------------------------------------------

    def toggle_multi_select(self) -> None:
        """Flip multi-select mode. Turning *off* abandons the current selection (clears the
        set); turning *on* starts with an empty set."""
        self._multi_select_active = not self._multi_select_active
        if not self._multi_select_active:
            self._selected_ids.clear()
        self._on_selection_changed()
        self.emit(self.dirty)

    def toggle_current_selection(self) -> None:
        """Flip membership of the cursor's item in the selection. No-op outside multi-select
        or on an empty window."""
        item_id = self._cursor_item_id()
        if item_id is None or not self._multi_select_active:
            return
        if item_id in self._selected_ids:
            self._selected_ids.discard(item_id)
        else:
            self._selected_ids.add(item_id)
        self._on_selection_changed()
        self.emit(self.dirty)

    def add_current_to_selection(self) -> None:
        """Idempotent add of the cursor's item — the half of ``toggle_current_selection``
        that ``shift+up`` / ``shift+down`` uses for range-select sugar. Held-key repeat
        across already-selected rows is a no-op, which is the right behaviour for sweeping
        the cursor through an extending range."""
        item_id = self._cursor_item_id()
        if item_id is None or not self._multi_select_active:
            return
        if item_id in self._selected_ids:
            return
        self._selected_ids.add(item_id)
        self._on_selection_changed()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Lifecycle helpers — concrete VM calls these at the right moments
    # ------------------------------------------------------------------

    def _clear_selection(self) -> None:
        """Drop ``_selected_ids`` and notify. No-op when already empty. Use from mutators
        that reshuffle the window (sort, filter, search): selection-by-position loses
        meaning after a reshuffle."""
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self._on_selection_changed()

    def _intersect_selection_with_visible_ids(self, visible: set[int]) -> None:
        """Drop any selected ids no longer present in the visible window. Use from a post-
        refetch ``on_complete`` callback so a bulk action doesn't leave behind ids that the
        new window can't render."""
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
