# rhizome/tui/widgets/browser/choices/

Shared base for browser-tab dialogs that present a navigable list of
named choices — Accept/Cancel, Confirm/Cancel, edit-action picker,
relink confirm, and similar. Captures the cursor + arrow nav +
enter/escape kernel that every dialog of this shape was reimplementing.

## Files

- **view.py** — `ChoiceList(Static, Generic[VM], can_focus=True)`.
  Owns the cursor, the arrow / enter / escape bindings, the standard
  `► label` (cursor) / `  label` (other) rendering, and the focus-
  brightness tracking. Subclasses customise via:

  - `CHOICES: dict[str, str]` — class-level `{label: action_method_name}`.
    On `enter` the widget resolves the cursor's label, looks up the
    action name, and invokes `getattr(self, action_name)()`
    (sync or async, both supported). Mirrors Textual's own
    `BINDINGS` action-string convention.
  - `choices()` — method override returning the same dict shape for
    dynamic choice lists (e.g. multi-select-dependent options).
  - `ORIENTATION` — `"horizontal"` (three spaces between choices) or
    `"vertical"` (newline between choices).
  - `LEAD: str | None` / `HINT: str | None` — static inline lead and
    bottom hint, both rendered `dim`. Override `_render_lead()` /
    `_render_hint()` for dynamic content.
  - `_render_header()` — optional line(s) *above* the choices (e.g.
    destructive-confirm prose).
  - `_render_choice(label, selected)` — override the per-choice
    rendering itself when a subclass wants a different visual (e.g.
    `_EditBar` uses colour-only without the `►` marker).
  - `action_cancel()` — what `escape` does (default no-op; subclasses
    typically call `vm.cancel()` or `self._tab.hide_dialog()`).

## Conventions

- **Sibling-dialog swap keys bubble.** `s` / `f` / `e` / `d` are *not*
  bound on `ChoiceList` — they bubble to the parent tab's BINDINGS,
  which owns the dialog mutex. Same pattern as `SortDialog`.
- **Cursor lives on the widget, not the VM.** The cursor is pure UI
  state with no data-model meaning. Don't push it onto the VM.
- **No VM mixin.** Action methods vary too much across consumers to
  justify a centralized contract. Each subclass wires its own action
  methods directly against whatever VM and parent-widget API it needs.
- **`prepare_for_show` resets the cursor** to choice 0 on each open,
  so a fresh open lands on the most-likely-default action (typically
  Accept / Confirm) regardless of where the user left it last time.
