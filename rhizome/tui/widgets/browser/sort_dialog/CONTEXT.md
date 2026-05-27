# rhizome/tui/widgets/browser/sort_dialog/

Shared sort-axis picker dialog for browser tabs. Used by widgets whose
backing VM exposes a sort axis + direction the user can change.

## Files

- **view.py** — `SortDialog(Static, Generic[VM], can_focus=True)`.
  Generic on the VM type, bound to `SortableViewModelMixin`. Renders a
  horizontal row of axes (from `vm.sort_options()`) with the active
  one decorated with an arrow + brackets and the cursor option in a
  bold accent colour, plus a keybinding hint row. Keys: `left` /
  `right` move the cursor (with wrap); `enter` applies (toggles
  direction when on the active axis, otherwise switches to that axis
  ascending); `r` resets to `sort_options()[0]` ascending; `escape`
  dismisses via the constructor-supplied `on_close` callback. Sibling-
  dialog swap keys (`d` / `s` / `f` / `e`) are deliberately *not*
  bound here — they bubble to the parent's BINDINGS, which owns the
  dialog mutex.

  The base widget exposes a `_extra_hint() -> Text | None` hook
  appended inline to the keybinding hint row. Default returns `None`;
  subclasses override to surface state-driven warnings without
  cluttering the mixin's contract.

- **view_model_mixin.py** — `SortableViewModelMixin(ViewModelBase,
  Generic[SortKey])`. Concrete VMs declare four members: the abstract
  `sort_options() -> tuple[SortKey, ...]` (display order; first
  element is the reset target), abstract `sort_by` and `sort_dir`
  properties for current state, and abstract `set_sort(sort_by,
  sort_dir)` to apply. Generic on the sort-key type so concrete VMs
  can narrow to their own `Literal[...]` alphabet.

## Conventions

- **Mix in at the leaf, not on an intermediate base.** Only the
  concrete VM that actually drives a `SortDialog` mixes in
  `SortableViewModelMixin` — never `QueryBackedViewModel`,
  `BrowserTabViewModel`, or any shared parent. Same convention as
  `SearchableViewModelMixin` (see `widgets/search_input/CONTEXT.md`).
- **State-driven warnings via subclass, not mixin extension.** When a
  consumer needs to surface an inline warning beyond the keybinding
  hints (e.g. "this action clears your selection"), subclass
  `SortDialog` and override `_extra_hint`. The mixin's contract stays
  narrow; the warning lives next to the consumer.
- **Parent owns the dialog slot and the mutex.** The dialog is
  rendered inside a parent-supplied slot whose CSS (height, margin,
  border-top) lives on the parent. `on_close` hands dismissal back to
  the parent — the dialog never reaches into the parent to manipulate
  visibility or focus.
