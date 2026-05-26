# rhizome/tui/widgets/browser/knowledge_entry_tab/

The first concrete `BrowserTabViewModel` implementation — a paginated
`DataTable` of `KnowledgeEntry` rows alongside an editable details panel.
See the parent `browser/CONTEXT.md` for the orchestrator + tab-base
contract; see `../braindump.md` for the long-form design rationale.

## Layout

Mirrors the parent `browser/` MVVM convention recursively — every
component is a `view.py` / `view_model.py` pair, with nested MVVM
components living in their own subdirectories.

- **view_model.py — `KnowledgeEntryBrowserTabViewModel`**: subclasses
  `BrowserTabViewModel`. Owns the windowed entries list, `_total`,
  `_has_more`, search/sort/entry-type filter state, the row cursor,
  multi-select state (`_multi_select_active`, `_selected_ids`), and a
  child `EntryDetailsViewModel` exposed via `self.details`. Also
  exports `DEFAULT_PAGE_LIMIT = 500`.

  **Scope discipline:** the VM owns *data facts*. Dialog UI state —
  which dialog is open, dialog cursors, the EDIT_OPTIONS list,
  multi/single distinction in the option set — lives in the view side.
  The VM's bulk-action surface is the four mutators
  (`set_sort`, `set_type_filter`, `delete_selected_entries`,
  `change_topic_on_selected_entries`,
  `change_type_on_selected_entries`); the view picks values and calls
  them. See the parent `CONTEXT.md` for the fetch behaviour notes
  (windowed fetch + count, cursor-doesn't-emit-dirty,
  `_on_details_saved` repaint, etc.).

- **view.py — `KnowledgeEntryBrowserTabView`**: `Vertical` containing a
  `Horizontal #tab-body` and a docked one-line status row. The body
  splits into a 60% `#table-column` (a `Vertical` housing the
  `_SearchInput` over the entries `DataTable`) and a 40%
  `EntryDetailsView`. The DataTable is a thin `_EntriesTable`
  subclass that owns the `m` (toggle multi-select) / `space` (toggle
  current row) keybindings. Implements the tab-view focus contract
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

Excluded from the tab's `alt+left/right` focus walk for now; the
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

The tab VM pushes the new mode + selection count into the details VM
via `set_multi_select(active, count)` on every toggle. The details VM
flips the TextAreas to read-only and hides the Accept/Cancel choices
list — the title/content of the cursor's entry stay visible (the
cursor still drives `set_entry`) but the user can't make edits until
they exit multi-select.

The same mode toggle also re-syncs the linked-flashcards panel. In
single-select the panel queries flashcards linked to the cursor entry
(one-element set); in multi-select it queries the union over
`_selected_ids` (deduped — a flashcard linked to multiple selected
entries appears once). An empty selection in multi-select mode is a
legal terminal state and renders an empty panel with a "no entries
selected" status line. The sub-VM's `set_entry_ids(frozenset)` is
idempotent, so cursor moves while multi-select is on are no-ops at
the sub-VM (the selection set didn't change), and the redundant
sync call from `set_cursor` costs nothing.

## Relink mode

A second "selection" mode that lives on the **linked-flashcards panel**
(`LinkedFlashcardsPanelViewModel`) rather than the entries side. Used to
pick which flashcards should remain linked to the current entry; the
relink-set semantic is "selected = linked".

**Precondition**: single-select on the entries tab. Relink doesn't make
sense over a multi-entry union, so the tab VM enforces this — entering
relink drops multi-select, and toggling multi-select on (`m`) exits any
active relink.

**Entry**: `l` on the entries table. Combined-motion via
`tab.toggle_relink_mode()` → `vm.enter_relink_mode()`: drops multi-select
if on, transitions to `LINKED_FLASHCARDS` if not already, turns on relink
on the panel sub-VM. The view wrapper also closes any open dialog (mutex
lives on the view side). Pressing `l` again is a toggle: routes to
`vm.exit_relink_mode()`, which turns off relink on the sub-VM but stays
in `LINKED_FLASHCARDS`. `ctrl+f` exits all the way back to `ENTRIES`
(its existing job); `transition_to(ENTRIES)` auto-exits relink as
cleanup since the panel is no longer visible.

### Dual-section model

Relink mode reshapes the panel from "just the linked flashcards" into a
two-section layout: the **pinned** section (flashcards currently linked
to the cursor entry) followed by a **remaining pool** (all other
flashcards within the parent tab's topic filter, deduped against the
pinned set, paginated). The pinned section is frozen at fetch time —
toggling a pinned row's selection off marks it for unlink but **does
not reposition the row**. The partition stays stable so the eventual
commit-relink action can diff against the originally-linked set.

State on `LinkedFlashcardsPanelViewModel`:

- `_linked_flashcards: list[Flashcard]` — pinned section. Always shown.
- `_remaining_flashcards: list[Flashcard]` — pool, relink-only. Empty
  outside relink.
- `_remaining_total: int | None`, `_remaining_has_more: bool` —
  pagination bookkeeping for the pool.
- `_topic_filter: frozenset[int] | None` — pushed down from the parent
  tab (see "Topic filter propagation" below). Scopes the pool only;
  the pinned section ignores it.
- `_search: str` — in non-relink filters the pinned section (existing
  behaviour); in relink filters the pool only. The pinned section stays
  unconditionally visible so the partition keeps its meaning mid-search.
- `_relink_mode`, `_relink_selected_ids` — flag + the selection set.
- `_cursor: int` — index into the combined display
  `[*linked, <boundary>, *remaining]` in relink mode, or just `linked`
  outside. `cursor_section()` and `cursor_flashcard` resolve which
  section/row the cursor sits on.

### Fetch protocol

Inherits from `QueryBackedViewModel` (see `widgets/CONTEXT.md`) — gets
the 50ms debounce + fetch-id staleness machinery for free. The earlier
"no debounce, queries are sub-ms" assumption broke once the pool query
landed; the kernel is now shared with the entry tab.

`_fetch` runs the linked query first, then keys the pool query off the
resulting linked ids via the `exclude_ids` parameter. Two sessions
because of the data dependency, but each is a single windowed SELECT (+
COUNT for the pool). Outside relink the pool query is skipped.

`load_more` extends the remaining pool only — the pinned section
doesn't paginate. Capture pattern matches the rest of the codebase:
`my_id = self._fetch_id`, run the windowed SELECT, check
`_still_current(my_id)` before applying.

### Topic filter propagation

`KnowledgeEntryBrowserTabViewModel.set_topic_filter` is overridden to
push the filter down to the panel sub-VM before calling
`super().set_topic_filter`. Order doesn't matter for correctness (both
ends debounce + reconcile via their own fetch ids); pushing the panel
first avoids a brief moment where the tab queries with the new filter
while the panel is still on the old one.

The panel's `set_topic_filter` is idempotent and refetches only when
relink mode is on (the pool query depends on it). Outside relink the
filter is stashed for the eventual relink entry.

### Selection baseline

On enter and on every successful `_process_fetched_data` while in mode,
`_relink_selected_ids` reseeds to the **linked section's** ids only —
those represent "currently linked → stay linked by default". Pool rows
start unselected; the user opts them in explicitly via `space`. Cursor
moves on the entries side (single-select only) trigger a fresh fetch
that reseeds with the new entry's linked set.

### View

The panel renders four columns (`sel` / `id` / `question` / `answer`).
In relink mode:

- Pinned linked rows render first with `[x]` / `[ ]` markers.
- A boundary row separates the sections — all cells render `─` glyphs
  in dim grey. The cursor **lands** on it but
  `toggle_current_relink_selection` no-ops there (VM guard via
  `cursor_section() == "boundary"`).
- Remaining pool rows render after the boundary.

Three colour regimes (mirrors entries table):
- non-relink: zebra-pair text (odd rows dim).
- relink, not selected: darker zebra pair (`-relink` CSS class on the
  table flips the palette).
- relink, selected: bold green (`#5fd75f`) to pop against the dimmed
  sea.

Auto-load-more fires from `_LinkedFlashcardsTable.action_cursor_down`
when the cursor is at the bottom edge and `remaining_has_more` is true.
The status line in relink shows
`"relink: N selected · pool {loaded}/{total} (l to exit, space to
toggle)"`.

### Accept / Cancel

When the relink selection diverges from the originally-linked
baseline (`is_relink_dirty` on the panel VM), a focusable
Accept/Cancel widget reveals between the table and the answer
preview. Mirrors the entry-details Accept/Cancel pattern: a
`_RelinkChoicesList(Static, can_focus=True)` mounted unconditionally
in `compose`, with its visibility toggled via the `.-visible` CSS
class in `_refresh` based on `vm.is_relink_dirty`.

Bindings: `←` / `→` move the cursor (VM-owned via
`relink_choice_cursor`), `enter` dispatches `vm.accept_relink()` or
`vm.cancel_relink()` by cursor position, `esc` is a shortcut for
Cancel.

The cursor (0 = Accept, 1 = Cancel) is reset to Accept on every
selection toggle so the user picks up the most likely action.

Reachable via `alt+left` / `alt+right` from the entries side: the
focus walk goes table → choices (only when visible) → end. Returning
left from choices lands back on the table; returning left from the
table escapes the panel back to the entries side.
**Focus-orphan rescue**: a dirty→clean transition (e.g. after
`accept_relink` exits relink mode or `cancel_relink` reverts) hides
the choices widget out from under any focus that was on it; the
panel's `_refresh` detects the transition and re-routes focus to the
table.

**Behavior**:

- **Cancel relink**: reverts `_relink_selected_ids` to the baseline
  (= ids of the currently-linked flashcards in the pinned section).
  Stays in relink mode so the user can keep working.
- **Accept relink**: computes the diff against the baseline, applies
  it via `link_flashcards_to_entry` /
  `unlink_flashcards_from_entry` (both insert/delete on
  `FlashcardEntry`, both idempotent against existing/missing rows),
  commits, then calls `exit_relink_mode()`. Exiting triggers a
  refetch via `_request_fetch`, which rebases the linked section to
  the new DB state. Relink is a single-select-only mode so
  `_entry_ids` must hold exactly one id; if the invariant is
  violated the method logs a warning and bails without touching the
  DB. `action_choice_confirm` on the view side is async to await
  the accept path.

Toggling selection itself remains a pure VM mutation (`space` flips
membership in `_relink_selected_ids` and recomputes
`is_relink_dirty`) — no DB I/O until accept.

## Help section

A bottom-of-tab help section toggled by `h` on the entries table.
**View-side state** (`_help_visible: bool` on
`KnowledgeEntryBrowserTabView`) — pure UI with no data-model
meaning, so it lives on the view rather than the VM. Mirrors the
`FlashcardReview` help pattern visually:

- A 1-line `#tab-help-hint` always sits at the very bottom, right-
  aligned. Shows `"h  show help"` (dim, with `h` bolded) when
  collapsed; blanks while expanded.
- A `#tab-help` Static directly above the hint expands when
  `_help_visible` flips on (`.-visible` CSS class), centered, listing
  the non-obvious bindings in a compact horizontal row joined by
  4 spaces.

`toggle_help` skips `vm.dirty` entirely and updates the two widgets
directly via `_refresh_help`. Initial paint happens in `on_mount`
since the hint exists independent of any VM state. The content list
covers global mode/navigation keys (`h`, `m`, `space`, `shift+↑/↓`,
`d/s/f/e`, `ctrl+f`, `l`, `alt+←/→`); per-dialog keys are
documented inside the dialog widgets themselves.

## Dialog orchestration

The four pop-up dialogs (delete / sort / filter / edit) all share one
screen slot — only one is visible at a time. The mutex lives on the
**view side** in `KnowledgeEntryBrowserTabView`: a single
`_active_dialog: Literal["delete","sort","filter","edit", None]`
attribute plus three methods (`show_dialog`, `hide_dialog`,
`toggle_dialog`) that toggle the `-visible` class on the right widget
and run focus rescue. The entries table's `d` / `s` / `f` / `e`
bindings call `tab.toggle_dialog(name)`; each dialog's own
sibling-swap bindings (e.g. pressing `s` inside `_DeleteConfirm`) also
go through `toggle_dialog`.

Each dialog widget owns its own cursor as a local attribute (no
`_*_pending` / `_*_cursor` on the VM) and exposes a `prepare_for_show`
hook the tab calls before revealing it — used to land the cursor on
a sensible default (e.g. the currently-active sort axis when the sort
bar opens). State transitions auto-dismiss any open dialog
(`_refresh` detects a `state != _last_state` and calls `hide_dialog`).

### Filter

Pressing `f` opens `_FilterDialog`. It surfaces two filter axes
stacked vertically:

- **Row 0 — `filter by type:`** (multi-select): a horizontal
  `[x] fact   [ ] exposition …` row backed by `vm.entry_types`.
  `None` means all types selected (no filter); a tuple restricts.
  `space` flips the cursor's option and calls `vm.set_type_filter`,
  collapsing back to `None` if every type ends up selected.
- **Row 1 — `filter by flashcards:`** (mutually exclusive radio):
  `( ) Any   ( ) No flashcards   (•) None`, backed by
  `vm.has_flashcards` (`True` / `False` / `None`). `space` makes the
  cursor's option the active value via `vm.set_flashcard_filter`.
  The explicit `None` option lets the user step back to no-filter
  without a separate "off" key.

Navigation: `↑` / `↓` move the cursor between rows (column clamps to
the new row's length); `←` / `→` move within a row (wrap). There's
no separate "apply" key — toggles push to the VM immediately. `r`
clears **both** axes at once (resets the dialog cursor to row 0 /
col 0). `f` / `escape` dismiss; `s` / `e` swap.

Both VM mutators (`set_type_filter`, `set_flashcard_filter`) clear the
selection (same rationale as the sort dialog — a different filter is
a different `LIMIT 500` window) and trigger a refetch via
`_request_fetch`. The active filters are projected onto the DB op's
`entry_types` (list of `EntryType` enums, or `None` for no filter;
empty list = "no rows match") and `has_flashcards` (`bool | None` —
`True` ⇒ EXISTS on `flashcard_entry`, `False` ⇒ NOT EXISTS, `None`
skipped). Filter state lives entirely in the VM and persists across
dialog open/close cycles.

The widget is intentionally not generalized over an abstract
"category" list — the previous `FilterCategoryViewModel` /
`MultiSelectFilterViewModel` hierarchy was speculative generality
that never paid off. Adding a third filter axis (text-CONTAINS, date
range, etc.) follows the pattern used to add the flashcards row:
extend the dialog's render + keystroke dispatch, add a new VM
mutator + property, thread a new kwarg through `_query_kwargs` /
`_apply_entry_filters`.

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

Pressing `d` opens `_DeleteConfirm`, mounted between the tab body
and the docked status line. The dialog reads `tab.selection_target_count()`
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

On `enter`, `_EditBar.action_select` calls `tab.handle_edit_choice(opt)`
with the option string. The tab dispatches:

- `change topic` / `change type` push a modal screen
  (`TopicSelectorScreen` / `_TypePickerScreen`); on dismiss, the
  selected value is applied via
  `vm.change_topic_on_selected_entries` / `vm.change_type_on_selected_entries`.
  On cancel, the edit bar refocuses so the user can pick a different
  action without re-pressing `e`.
- `edit title` / `edit content` hide the edit bar and focus the
  corresponding TextArea in the details panel.
- `delete` swaps to the delete confirm dialog
  (`tab.show_dialog("delete")`).

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

The tab VM's `update_entry` op gained a `topic_id` keyword argument
to support the topic-change path (non-nullable on the model, so
`None` unambiguously means "skip" — same as every other field).

- **entry_details/ — `EntryDetailsView` + `EntryDetailsViewModel`**:
  buffered-edit side panel. Its own MVVM subdirectory; see its
  `CONTEXT.md`.
