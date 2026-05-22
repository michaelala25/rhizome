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
  `Horizontal #pane-body` (table at 60%, `EntryDetailsView` at 40%) and a
  docked one-line status row. The DataTable is a thin `_EntriesTable`
  subclass that owns the `m` (toggle multi-select) / `space` (toggle
  current row) keybindings. Implements the pane-view focus contract
  (`focus_first` / `focus_next_region` / `focus_prev_region`) and
  delegates the details region's internal cycle to `EntryDetailsView`.
  `focus_next_region` short-circuits the table → details transition
  while multi-select is active so `alt+right` keeps focus on the table.

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

### Bulk delete

While multi-select is on with a non-empty selection, pressing `d`
opens a confirm dialog mounted between the pane body and the docked
status line (`_DeleteConfirm` widget; mirrors the design of
`_ChoicesList` in the details panel). Up/down moves the
Confirm/Cancel cursor; enter dispatches; escape dismisses without
deleting. The dialog grabs focus on appear and returns it to the
table on dismiss (open/close transitions detected via
`_was_delete_pending` in the pane view, mirroring `_was_dirty` in
`EntryDetailsView`). `focus_first` also re-focuses the dialog when
it's open, so a tree side-trip via alt+left then alt+right lands the
user back on the dialog.

On confirm, the pane VM bulk-deletes each entry via `delete_entry`
inside a single session + commit. The FK on `flashcard_entry.entry_id`
cascades, so flashcard-to-entry link rows are cleaned up automatically
but the flashcards themselves are unaffected — which is what the
dialog promises the user. After the commit, the VM prunes
`self._entries`, decrements `self._total`, clears `_selected_ids`,
and clamps the cursor; no refetch. Multi-select mode stays on so the
visual context is preserved — the user hits `m` to exit when done.

Pressing `d` is a no-op outside multi-select, with an empty
selection, or when the dialog is already open. Toggling multi-select
off while the dialog is open dismisses it (the selection is about to
be cleared anyway).

- **entry_details/ — `EntryDetailsView` + `EntryDetailsViewModel`**:
  buffered-edit side panel. Its own MVVM subdirectory; see its
  `CONTEXT.md`.
