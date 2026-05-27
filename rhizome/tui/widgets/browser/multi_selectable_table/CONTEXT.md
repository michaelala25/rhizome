# rhizome/tui/widgets/browser/multi_selectable_table/

Shared multi-select scaffolding for browser-tab tables: a `DataTable` subclass paired with
a VM mixin. The widget is a thin keybinding shim; the mixin owns the selection-set state
machine.

## Files

- **view.py** — `MultiSelectableDataTable(DataTable, Generic[VM])`. `space` /
  `shift+up` / `shift+down` keybindings route to VM mutators. Module docstring covers
  the keybinding contract.
- **view_model_mixin.py** — `MultiSelectableViewModelMixin(ViewModelBase)`. Selection-set
  state machine plus the abstract surface and lifecycle hooks the concrete VM wires up.
  Module docstring covers the contract.

## Current consumer

`knowledge_entry_tab/` — `_EntriesTable` subclasses `MultiSelectableDataTable`; the tab VM
mixes in `MultiSelectableViewModelMixin`.

## Conventions

- **Mix the VM mixin in at the leaf VM**, not on an intermediate base. Same rule as the
  sibling Searchable/Sortable mixins.
- **Visual rendering stays per-tab.** The shared widget paints nothing — no marker column,
  no row highlight, no zebra wash. Each tab's `_refresh` reads `vm.multi_select_active` /
  `vm.is_selected(id)` and renders to taste.
- **Pagination is orthogonal.** Auto-load-more on cursor-down is not bundled here;
  subclasses layer it via their own `action_cursor_down` override.
- **Override `toggle_multi_select` for before-flip side effects** (e.g. exiting a
  mutually-exclusive sub-mode); use `_on_selection_changed` for after-state-change syncing.
