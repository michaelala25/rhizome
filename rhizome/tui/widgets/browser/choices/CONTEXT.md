# rhizome/tui/widgets/browser/choices/

Shared base for browser-tab dialogs that present a navigable list of named choices
(Accept/Cancel, edit-action pickers, relink confirms, etc.).

## Files

- **view.py** — `ChoiceList(Static, Generic[VM])`. Cursor + arrow nav + enter/escape +
  rendering. See its module docstring for the `CHOICES` dispatch contract and subclass hooks.

## Conventions

- **Sibling-dialog swap keys (`d` / `s` / `f` / `e`) bubble** to the parent tab's BINDINGS,
  which owns the dialog mutex. The user can swap between sibling dialogs without dismissing
  the current one first.
- **Cursor is widget state**, not VM state — it has no data-model meaning.
- **No VM mixin**: action methods vary too much across consumers to justify a shared contract.
  Each subclass wires actions directly against whatever VM/parent surface it needs.
