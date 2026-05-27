# rhizome/tui/widgets/search_input/

Shared, VM-agnostic search-box widget. Used by widgets whose backing VM
exposes a `set_search(query)` mutator and wants the standard "single-line
input, `enter` submits, `esc` × 2 clears" UX with the hint riding the top
border.

## Files

- **view.py** — `SearchInput(Input, Generic[VM])`. Generic on the VM
  type, bound to `SearchableViewModelMixin`. The widget treats the VM as
  a black box that accepts `set_search(query)`; nothing VM-specific
  leaks across the boundary. Owns the local "armed for clear" state
  machine so two consecutive escape presses clear the buffer while a
  single escape followed by any other keystroke disarms. Border title
  doubles as the hint surface (`enter to submit • esc × 2 to clear`),
  swapped to a red prompt while armed.
- **view_model_mixin.py** — `SearchableViewModelMixin(ViewModelBase)`.
  Abstract `set_search(query)` declared here so the type bound on the
  generic widget is satisfied without imposing a parallel base
  hierarchy on concrete VMs. The mixin adds no state; it inherits
  `ViewModelBase.__init__` unchanged so it slots into a cooperative
  multiple-inheritance MRO with any other `ViewModelBase` ancestor.

## Conventions

- **Mix in at the leaf, not on an intermediate base.** Only the concrete
  VM that actually drives a `SearchInput` should mix in
  `SearchableViewModelMixin` — never `QueryBackedViewModel`,
  `BrowserTabViewModel`, or any other shared parent. This keeps
  unrelated VMs out of the abstract obligation and keeps the MRO local
  to the one class that needs it.
- **Spell out the type bound at construction.** Instances are
  constructed as `SearchInput[ConcreteVM](vm, id=...)` so the
  type-checker can keep the VM-typed `self._vm` attribute accurate at
  call sites.
- **Style via the class selector, not the id.** CSS rules attached to
  the widget's chrome live in `DEFAULT_CSS` on `SearchInput` itself —
  the per-instance `id` is for layout-level rules and focus routing in
  the parent panel.
