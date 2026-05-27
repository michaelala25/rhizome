# rhizome/tui/widgets/browser/multi_selectable_table/

Shared multi-select scaffolding for browser-tab tables. Pairs a `DataTable`
subclass (just the keybinding shim) with a VM mixin (the small selection-set
state machine).

## Files

- **view.py** — `MultiSelectableDataTable(DataTable, Generic[VM])`. Adds
  three keybindings — `space` toggles the cursor row, `shift+down` /
  `shift+up` are range-select sugar (idempotent add + cursor step). All
  three are no-ops outside multi-select (the VM guards). The widget owns
  no state beyond a VM reference; auto-load-more on cursor-down is a
  separate concern that concrete subclasses can add.

- **view_model_mixin.py** — `MultiSelectableViewModelMixin(ViewModelBase)`.
  Owns the `multi_select_active` flag, the id-keyed `_selected_ids` set,
  and the three mutators (`toggle_multi_select`,
  `toggle_current_selection`, `add_current_to_selection`) plus the
  `selected_target_ids()` resolver and the two lifecycle helpers
  (`_clear_selection`, `_intersect_selection_with_visible_ids`).

  **Concrete VM contract:**
  - `_selectable_items() -> list` — current windowed list
  - `_item_id(item) -> int` — extract the selection-set key from an item
  - `cursor` (property) — current row index into the window
  - `_on_selection_changed()` — optional hook to thread state down to
    sub-VMs / sibling UI (e.g. push the new state to a details VM or
    re-sync a linked-flashcards sub-VM's target set). Argless; reads VM
    state directly.

  **Lifecycle obligations on the concrete VM:**
  - Call `_clear_selection()` from any mutator that reshuffles the
    window (sort, filter, search changes) — selection-by-position
    loses meaning after a reshuffle.
  - Call `_intersect_selection_with_visible_ids({...})` from a post-
    refetch `on_complete` callback so a bulk action doesn't leave
    behind ids the new window can't render.

## Conventions

- **Mix in at the leaf, not on an intermediate base.** Same rule as
  `SearchableViewModelMixin` / `SortableViewModelMixin`. Future browser
  tabs that want multi-select opt in by adding the mixin to their
  concrete VM directly.

- **Visual rendering stays per-tab.** The widget paints no marker
  column, no selected-row highlight, no zebra wash — those are
  domain-specific rendering decisions (which colour to use for
  "selected", whether to show `[x]`/`[ ]` markers, whether to dim
  non-selected rows). The parent view's `_refresh` handles them by
  reading `vm.multi_select_active` / `vm.is_selected(id)`.

- **Pagination is orthogonal.** `MultiSelectableDataTable` does not
  include auto-load-more on cursor-down. Concrete subclasses that
  want pagination override `action_cursor_down` themselves (e.g. the
  entries tab's `_EntriesTable`). If a pattern emerges across several
  consumers, lift it into a sibling `PaginatedDataTable` or a
  load-more mixin — but not bundled in here.

- **`toggle_multi_select` is overridable.** Subclasses that need
  side-effects when entering / exiting multi-select (e.g. exiting a
  mutually-exclusive sub-mode like relink) override the method and
  call `super().toggle_multi_select()`. The mixin's `_on_selection_changed`
  hook covers the "state changed; sync derived UI" half; the override
  covers the "before flipping, also do X" half.
