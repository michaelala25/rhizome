# rhizome/tui/widgets/browser/knowledge_entry_pane/

The first concrete `BrowserPaneViewModel` implementation — a paginated
`DataTable` of `KnowledgeEntry` rows alongside an editable details panel.
See the parent `browser/CONTEXT.md` for the orchestrator + pane-base
contract; see `../braindump.md` for the long-form design rationale.

## Layout

Mirrors the parent `browser/` MVVM convention recursively — every
component is a `view.py` / `view_model.py` pair, with nested MVVM
components living in their own subdirectories.

- **view_model.py — `KnowledgeEntryBrowserPaneViewModel`**: subclasses
  `BrowserPaneViewModel`. Owns the windowed entries list, `_total`,
  `_has_more`, search/sort/entry-type filter state, the row cursor,
  multi-select state (`_multi_select_active`, `_selected_ids`), and a
  child `EntryDetailsViewModel` exposed via `self.details`. Also
  exports `DEFAULT_PAGE_LIMIT = 500`.

  **Scope discipline:** the VM owns *data facts*. Dialog UI state —
  which dialog is open, dialog cursors, the EDIT_OPTIONS list,
  multi/single distinction in the option set — lives in the view side.
  The VM's bulk-action surface is the four mutators
  (`set_sort`, `apply_filter`, `delete_selected_entries`,
  `change_topic_on_selected_entries`,
  `change_type_on_selected_entries`); the view picks values and calls
  them. See the parent `CONTEXT.md` for the fetch behaviour notes
  (windowed fetch + count, cursor-doesn't-emit-dirty,
  `_on_details_saved` repaint, etc.).

- **view.py — `KnowledgeEntryBrowserPaneView`**: `Vertical` containing a
  `Horizontal #pane-body` and a docked one-line status row. The body
  splits into a 60% `#table-column` (a `Vertical` housing the
  `_SearchInput` over the entries `DataTable`) and a 40%
  `EntryDetailsView`. The DataTable is a thin `_EntriesTable`
  subclass that owns the `m` (toggle multi-select) / `space` (toggle
  current row) keybindings. Implements the pane-view focus contract
  (`focus_first` / `focus_next_region` / `focus_prev_region`) and
  delegates the details region's internal cycle to `EntryDetailsView`.
  `focus_next_region` short-circuits the table → details transition
  while multi-select is active so `alt+right` keeps focus on the table.

## Search

`_SearchInput` sits above the entries table inside `#table-column`.
Visually mirrors the entry-detail title field — 3-row tight box,
transparent background, `#3a3a3a` border that flips accent on focus.
The keybinding hint rides the top border on the right
(`border_title_align = "right"`): default state `enter to submit •
esc × 2 to clear` in dim; armed-for-clear state `press esc again to
clear` in bold red.

State flow: typing buffers the query locally; `enter` propagates to
`vm.set_search` which triggers a refetch via the existing
search/sort plumbing. `esc` arms a clear, the second `esc` blanks
the value and submits the empty query (the natural "no filter"
state). Any non-`esc` key disarms — sits inside `_SearchInput`
itself (rather than a parent wrapper) because Input consumes
character keystrokes before they bubble, so only the focused widget
sees the "user typed something" signal needed to disarm.

Excluded from the pane's `alt+left/right` focus walk for now; the
user engages the bar by clicking (or via Textual's default `tab`
focus order).

## Multi-select

The user toggles multi-select with `m` while the entries table is
focused; once on, `space` adds/removes the cursor row from the
selection set. Turning multi-select **off** abandons the selection
(clears `_selected_ids`) — there's no "preserve selection across mode
flips" affordance.

Selections are keyed by entry id rather than row index so they survive
`load_more` calls and filter/search/sort-driven refetches. The view
adds a leading "sel" column (always present, width 3) and renders
`[ ]` / `[x]` markers only while multi-select is active; selected rows
render bright green (`#5fd75f`), and the rest of the table shifts to a
darker zebra palette via a `-multi-select` CSS class on the
`DataTable`. The status line is replaced by a "multi-select: N
entries selected" hint while the mode is on.

The pane VM pushes the new mode + selection count into the details VM
via `set_multi_select(active, count)` on every toggle. The details VM
flips the TextAreas to read-only and hides the Accept/Cancel choices
list — the title/content of the cursor's entry stay visible (the
cursor still drives `set_entry`) but the user can't make edits until
they exit multi-select.

## Dialog orchestration

The four pop-up dialogs (delete / sort / filter / edit) all share one
screen slot — only one is visible at a time. The mutex lives on the
**view side** in `KnowledgeEntryBrowserPaneView`: a single
`_active_dialog: Literal["delete","sort","filter","edit", None]`
attribute plus three methods (`show_dialog`, `hide_dialog`,
`toggle_dialog`) that toggle the `-visible` class on the right widget
and run focus rescue. The entries table's `d` / `s` / `f` / `e`
bindings call `pane.toggle_dialog(name)`; each dialog's own
sibling-swap bindings (e.g. pressing `s` inside `_DeleteConfirm`) also
go through `toggle_dialog`.

Each dialog widget owns its own cursor as a local attribute (no
`_*_pending` / `_*_cursor` on the VM) and exposes a `prepare_for_show`
hook the pane calls before revealing it — used to land the cursor on
a sensible default (e.g. the currently-active sort axis when the sort
bar opens). State transitions auto-dismiss any open dialog
(`_refresh` detects a `state != _last_state` and calls `hide_dialog`).

### Filter

Pressing `f` opens `_FilterDialog`. It surfaces the entry-type filter
— the only filter today — as a horizontal `[x] fact   [ ] exposition
…` row. Selection state is derived directly from `vm.entry_types`:
`None` means all types selected (no filter); a tuple restricts.
There's no separate "apply" key — `space` flips the cursor's option
locally and calls `vm.apply_filter` immediately, collapsing back to
`None` if every type ends up selected. `r` resets to no filter via
`vm.apply_filter(None)`. `f` / `escape` dismiss; `s` / `e` swap.

`vm.apply_filter` clears the selection (same rationale as the sort
dialog — a different filter is a different `LIMIT 500` window) and
triggers a refetch via `_request_fetch`. The active filter is
projected to the DB op's `entry_types` parameter (list of `EntryType`
enums, or `None` for no filter; an empty list means "no rows match",
mirroring `topic_ids` semantics). Filter state lives entirely in the
VM and persists across dialog open/close cycles.

The widget is intentionally not generalized over an abstract
"category" list — the previous `FilterCategoryViewModel` /
`MultiSelectFilterViewModel` hierarchy was speculative generality
that never paid off. Adding a second filter axis (text-CONTAINS, date
range, etc.) is a future refactor: extend the dialog's render and
keystroke dispatch, extend `vm.apply_filter`'s signature.

### Sort

Pressing `s` opens `_SortBar`. The dialog surfaces four axes — `id`,
`title`, `type`, `topic` — mirroring the data table's column order
left-to-right. The cursor lands on the currently-active axis on open
(via `prepare_for_show`); `left` / `right` move with wrap; `enter`
applies, computing the toggle locally — same axis → flip direction,
different axis → switch ascending — and calls `vm.set_sort(by, dir)`.
`r` resets to `id` ascending. `s` / `escape` dismiss; `f` / `e` swap.

The active sort renders with an arrow + brackets (`↑[id]`); the
cursor option is shown in bold gold on focus / bold grey otherwise.

The DB op handles the two non-column axes: `type` uses a `CASE`
expression (locks the semantic order fact → exposition → overview
rather than the natural string sort which puts exposition first), and
`topic` joins onto the `Topic` table and orders on `lower(Topic.name)`
for case-insensitive alpha.

`vm.set_sort` clears the selection (a new sort means a new `LIMIT
500` window, and tracking selections across reshuffled windows is
more complexity than the feature warrants). The multi-select dialog
hint surfaces this in red while picking; the mode itself stays on,
just with an empty set.

### Delete

Pressing `d` opens `_DeleteConfirm`, mounted between the pane body
and the docked status line. The dialog reads `pane.selection_target_count()`
to render the header (`Delete N selected entries?` in multi-select,
`Delete 1 entry?` in single-select). Up/down moves the Confirm/Cancel
cursor (owned locally by the widget); enter dispatches; escape
dismisses; `s` / `f` / `e` swap.

On confirm the dialog awaits `vm.delete_selected_entries()` and then
hides itself. The VM resolves the target set via the internal
`_selected_target_ids` helper (the live `_selected_ids` set in
multi-select, the cursor entry in single-select) and deletes each via
`delete_entry` inside a single session + commit. The FK on
`flashcard_entry.entry_id` cascades, so flashcard-to-entry link rows
are cleaned up automatically but the flashcards themselves are
unaffected. After the commit, the VM prunes `self._entries`,
decrements `self._total`, clears `_selected_ids` (multi-select only),
and clamps the cursor; no refetch. Multi-select mode stays on so the
visual context is preserved — the user hits `m` to exit when done.

There's no longer a frozen-target mechanism — focus-based dismissal
handles the equivalent: while the dialog is focused, the table
doesn't see arrow keys, so the cursor stays put and the single-select
target naturally remains the same. `vm.delete_selected_entries`
no-ops when nothing's targetable.

### Edit

Pressing `e` opens `_EditBar`. The option list lives on the view side
as module-level constants:

- **multi-select** (`_EDIT_OPTIONS_MULTI`): `change topic` ·
  `change type` · `delete`
- **single-select** (`_EDIT_OPTIONS_SINGLE`): `change topic` ·
  `change type` · `edit title` · `edit content` · `delete`

`edit title` / `edit content` are excluded in multi-select because
the details panel is frozen and there's no single entry to refocus
onto. `delete` always sits last so the cursor never lands on the
destructive action without an explicit rightward step.

On `enter`, `_EditBar.action_select` calls `pane.handle_edit_choice(opt)`
with the option string. The pane dispatches:

- `change topic` / `change type` push a modal screen
  (`TopicSelectorScreen` / `_TypePickerScreen`); on dismiss, the
  selected value is applied via
  `vm.change_topic_on_selected_entries` / `vm.change_type_on_selected_entries`.
  On cancel, the edit bar refocuses so the user can pick a different
  action without re-pressing `e`.
- `edit title` / `edit content` hide the edit bar and focus the
  corresponding TextArea in the details panel.
- `delete` swaps to the delete confirm dialog
  (`pane.show_dialog("delete")`).

`_TypePickerScreen` is defined in the same `view.py` rather than
under `tui/screens/` — it's tiny and only used here, so the extra
indirection isn't worth it (lift it if a second consumer appears).

**Selection-preserving refetch.** `change_topic_on_selected_entries`
and `change_type_on_selected_entries` go through `_post_change_refetch`,
which kicks off a normal `_request_fetch` and passes
`_intersect_selection_with_window` as the `on_complete` callback.
The base class runs the callback synchronously right after
`_process_fetched_data` lands, so the intersection sees the fresh
window. Selections survive sort moves (entry still in window, just
at a different row) but get dropped when an entry falls outside the
active filter or gets pushed past the 500-row window by a reorder.
Edge: an entry that still matches the filter but lands past the
500-row window is also dropped from the selection — matches
`load_more`'s behaviour of not reaching back for selected-but-
unloaded rows.

The pane VM's `update_entry` op gained a `topic_id` keyword argument
to support the topic-change path (non-nullable on the model, so
`None` unambiguously means "skip" — same as every other field).

- **entry_details/ — `EntryDetailsView` + `EntryDetailsViewModel`**:
  buffered-edit side panel. Its own MVVM subdirectory; see its
  `CONTEXT.md`.
