# rhizome/tui/widgets/browser/sort_dialog/

Shared sort-axis picker for browser tabs. Current consumer: `knowledge_entry_tab` via
`_EntriesSortDialog`.

## Files

- **view.py** — `SortDialog(Static, Generic[VM])`: horizontal axis row + keybinding hint row.
  Subclass hook `_extra_hint()` for state-driven warnings. See the module docstring for the
  full key map and rendering rules.
- **view_model_mixin.py** — `SortableViewModelMixin(ViewModelBase, Generic[SortKey])`: the
  four-member VM contract (`sort_options`, `sort_by`, `sort_dir`, `set_sort`).

## Conventions

- Mix `SortableViewModelMixin` in at the **leaf** VM only — never on a shared base.
- Sibling-dialog swap keys (`d` / `s` / `f` / `e`) are deliberately not bound here; they
  bubble to the parent tab's BINDINGS, which owns the dialog mutex.
- State-driven warnings go through `_extra_hint` on a `SortDialog` subclass, not via
  extending the mixin contract.
- Parent owns the dialog slot (CSS, visibility) and receives dismissal through `on_close`.
