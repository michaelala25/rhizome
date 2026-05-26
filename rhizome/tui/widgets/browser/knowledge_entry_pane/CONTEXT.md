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
  `_has_more`, search/sort state, the row cursor, multi-select state
  (`_multi_select_active`, `_selected_ids`), and a child
  `EntryDetailsViewModel` exposed via `self.details`. Also exports
  `DEFAULT_PAGE_LIMIT = 500`. See the parent `CONTEXT.md` for the
  detailed behaviour notes (windowed fetch + count, cursor-doesn't-emit-
  dirty, `_on_details_saved` repaint, etc.).

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

### Filter

Pressing `f` while the entries table is focused opens a per-axis
filter picker (`_FilterDialog`) in the same screen slot as
`_SortBar` / `_DeleteConfirm`. The three dialogs are mutually
exclusive at the VM level (`request_filter` / `request_sort` /
`request_delete` each clear the others), and the view's `_refresh`
resolves focus once across all three after toggling visibility so
swaps like `f`-from-sort don't let the closing dialog's "restore
focus to table" overwrite the opening dialog's focus grab.

The dialog is built around an extensible list of `FilterCategoryViewModel`
subclasses. Today there's only one — a `MultiSelectFilterViewModel`
seeded with the three `EntryType` values — but the widget's
rendering and key dispatch both branch on `isinstance(category, …)`
so adding a new shape (e.g. a text-CONTAINS filter or a numeric
range) is a localized change: a new VM subclass plus one new branch
in `_FilterDialog._render_active_category` and the action handlers.
The top line of the dialog shows the category tabs (active one
bracketed, non-default categories tinted green); the second line
hosts whatever input shape the active category needs.

Keys: `tab` / `shift+tab` cycle categories (no-op with one); `←` /
`→` move the cursor within the active category; `space` toggles the
cursor's option (multi-select); `r` resets every category to
default; `s` swaps to the sort dialog; `f` / `escape` dismiss.
Pressing `f` from `_SortBar` or `_DeleteConfirm` swaps to the
filter dialog in one step — same priority pattern as the existing
`s` swap.

**Toggling clears any active selection** (same rationale as the sort
dialog — a different filter is a different `LIMIT 500` window). The
toggle itself triggers a refetch and a count update via the same
`_request_fetch` path the sort + topic-tree filters use; the active
type filter is projected to the DB op's new `entry_types` parameter
(`None` when at default = all selected, list of `EntryType` enums
otherwise; the DB op treats an empty iterable as "no rows" same as
`topic_ids`). Filter state persists across dialog open/close cycles
— the user can dismiss with `f` and reopen later to fine-tune.

### Sort

Pressing `s` while the entries table is focused opens a horizontal
sort-axis picker (`_SortBar`) mounted in the same screen slot as the
delete dialog (the VM enforces mutual exclusion — `request_sort`
dismisses any pending delete first; pressing `s` from inside the
delete dialog swaps dialogs in one step via a binding on
`_DeleteConfirm`).

The dialog surfaces four axes — `id`, `title`, `type`, `topic` —
mirroring the data table's column order left-to-right. `left` / `right`
move the cursor (with wrap); `enter` applies the highlighted axis,
toggling direction when it matches the current sort and otherwise
switching to that axis in ascending order; `s` / `escape` dismiss
without applying. The active sort renders with an arrow + brackets
(`↑[id]`); the cursor option is shown in bold gold on focus / bold
grey otherwise.

The DB op gets a small expansion to support the two non-column axes:
`type` uses a `CASE` expression (locks the semantic order
fact → exposition → overview rather than the natural string sort
which puts exposition first), and `topic` joins onto the `Topic` table
and orders on `lower(Topic.name)` for case-insensitive alpha.

**Applying a sort clears any active selection.** A new sort means a
new `LIMIT 500` window — different rows may end up in scope — and
tracking selections across windows that don't necessarily contain the
same entries is more complexity than the feature warrants. The
multi-select dialog hint surfaces this in red while picking; the mode
itself stays on, just with an empty set.

### Delete

Pressing `d` while the entries table is focused opens a confirm
dialog mounted between the pane body and the docked status line
(`_DeleteConfirm` widget; mirrors the design of `_ChoicesList` in
the details panel). The target set comes from the VM's
`delete_target_ids` computed property: the live `_selected_ids` in
multi-select mode, or `{entries[cursor].id}` in single-select mode
(frozen into `_delete_single_target_id` at `request_delete` time so
cursor moves while the dialog is open don't repoint the target).
Dialog copy adjusts: `Delete N selected entries?` in multi-select,
`Delete 1 entry?` in single-select. Up/down moves the
Confirm/Cancel cursor; enter dispatches; escape dismisses without
deleting; `s` / `f` / `e` swap to the corresponding sibling dialog.
The dialog grabs focus on appear and returns it to the table on
dismiss; `focus_first` also re-focuses the dialog when it's open, so
a tree side-trip via alt+left then alt+right lands the user back on
the dialog.

On confirm, the pane VM deletes each target entry via `delete_entry`
inside a single session + commit. The FK on `flashcard_entry.entry_id`
cascades, so flashcard-to-entry link rows are cleaned up automatically
but the flashcards themselves are unaffected — which is what the
dialog promises the user. After the commit, the VM prunes
`self._entries`, decrements `self._total`, clears `_selected_ids`
(multi-select only), and clamps the cursor; no refetch. Multi-select
mode stays on so the visual context is preserved — the user hits `m`
to exit when done.

Pressing `d` is a no-op when nothing is targetable (empty window in
single-select, empty selection in multi-select) or when the dialog
is already open. Toggling multi-select off while the dialog is open
dismisses it (the selection is about to be cleared anyway).

### Edit

Pressing `e` while the entries table is focused opens a horizontal
edit-action picker (`_EditBar`) sharing the screen slot with the
sort / filter / delete dialogs — the four are mutually exclusive at
the VM level (`request_edit` / `request_sort` / `request_filter` /
`request_delete` each clear the other three). Target resolution
mirrors delete: live `_selected_ids` in multi-select mode, frozen
`_edit_single_target_id` in single-select mode.

Option set depends on mode (exposed by `vm.edit_options`):

- **multi-select**: `change topic` · `change type` · `delete`
- **single-select**: `change topic` · `change type` · `edit title` ·
  `edit content` · `delete`

`edit title` / `edit content` are excluded in multi-select because
the details panel is frozen (read-only) and there's no single entry
for them to refocus onto. `delete` always sits last so the cursor
never lands on the destructive action without an explicit rightward
step.

Dispatch lives on the view side (`KnowledgeEntryBrowserPaneView.handle_edit_choice`)
because two of the choices need view-level affordances (modal screen
push for `change topic` / `change type`) and two are pure focus
shortcuts onto sibling widgets (`edit title` / `edit content` focus
the corresponding TextArea in the details panel and dismiss the
bar). The VM is only consulted for the cursor index and for applying
the chosen value (`apply_change_topic` / `apply_change_type`).

`change topic` pushes the existing `TopicSelectorScreen` (same
modal used by the commit-proposal widget); `change type` pushes a
local `_TypePickerScreen` defined in the same `view.py` rather than
under `tui/screens/` — it's tiny and only used here, so the extra
indirection isn't worth it (lift it if a second consumer appears).
Both screens dismiss with the picked value or `None`; on cancel,
the edit bar refocuses so the user can pick a different action
without re-pressing `e`. On apply, the change persists via
`update_entry` per target inside a single session + commit, then
the pane refetches.

**Selection-preserving refetch.** `apply_change_topic` and
`apply_change_type` go through `_post_change_refetch`, which kicks
off a normal `_request_fetch` and passes
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

`edit title` and `edit content` are sub-trivial: dismiss the edit
bar (via `cancel_edit`) and focus `#details-title` /
`#details-content` directly. The cancel triggers the pane's
`_refresh` which would normally route focus back to the table, but
the explicit `target.focus()` call afterwards wins.

The pane VM's `update_entry` op gained a `topic_id` keyword argument
to support the topic-change path (non-nullable on the model, so
`None` unambiguously means "skip" — same as every other field).

- **entry_details/ — `EntryDetailsView` + `EntryDetailsViewModel`**:
  buffered-edit side panel. Its own MVVM subdirectory; see its
  `CONTEXT.md`.
