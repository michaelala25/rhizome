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
  and a child `EntryDetailsViewModel` exposed via `self.details`. The
  multi-select state machine (mode flag + selection set) lives on the
  `MultiSelectableViewModelMixin` it inherits; see "Multi-select"
  below. Also exports `DEFAULT_PAGE_LIMIT = 500`.

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
  splits into a 60% `#table-column` (a `Vertical` housing the shared
  generic `SearchInput` over the entries `DataTable`) and a 40%
  `EntryDetailsView`. The DataTable is a thin `_EntriesTable`
  subclass that owns the entries-side selection keybindings (`space`
  toggles the cursor row, `shift+up` / `shift+down` extend the
  selection range). The **global tab keys** `d` / `s` / `f` / `e`
  toggle the four dialogs, `l` toggles relink, `m` toggles multi-
  select; these live on the tab class itself so they fire from either
  table. Each action gates on `_typing_active` to skip when an
  `Input` / `TextArea` is focused. Owns `focus_first` (entry point
  when `BrowserView` enters the tab from the tree) and the
  `nav_<dir>` graph walkers; see "Cross-region focus" below.

  Per-widget code lives in sibling modules — `delete_dialog.py`,
  `filter_dialog.py`, `edit_dialog.py`, `entry_content_preview.py`.
  Each owns the widget class plus the constants/helpers it uses (e.g.
  `_EDIT_OPTIONS_SINGLE` / `_EDIT_OPTIONS_MULTI` and
  `_TypePickerScreen` in `edit_dialog.py`, `_OneOfInput` /
  `_TYPE_OPTIONS` / `_FLASHCARD_OPTIONS` / `_parse_id_list` in
  `filter_dialog.py`). The dialog widgets reference the tab via
  `TYPE_CHECKING` to avoid an import cycle. The search bar is the
  shared generic `SearchInput` from
  `rhizome.tui.widgets.search_input`; `KnowledgeEntryBrowserTabViewModel`
  mixes in `SearchableViewModelMixin` to satisfy the widget's type
  bound, and the instance is constructed as
  `SearchInput[KnowledgeEntryBrowserTabViewModel](self._vm, …)`. The
  sort dialog is the shared generic `SortDialog` from
  `rhizome.tui.widgets.browser.sort_dialog`, specialised inline as
  `_EntriesSortDialog` (overrides `_extra_hint` to surface the
  "Applying clears your selection." warning while multi-select is on);
  `KnowledgeEntryBrowserTabViewModel` mixes in
  `SortableViewModelMixin[EntrySortKey]`. The instance is constructed
  with `on_close=self.hide_dialog`, decoupling the dialog from the
  tab's exact dismissal API.

  See "Cross-region focus" below for the full `alt+arrow` graph the
  tab implements via `nav_up` / `nav_down` / `nav_left` / `nav_right`.

## Search

The search bar above the entries table is an instance of the shared
generic `SearchInput` widget (see
`rhizome/tui/widgets/search_input/CONTEXT.md` for its full behaviour
contract — armed-for-clear escape state machine, border-title hint,
`enter` submits via `vm.set_search`). The instance is mounted as
`#search-input` inside `#table-column`.
`KnowledgeEntryBrowserTabViewModel` opts in by mixing in
`SearchableViewModelMixin` and implementing `set_search(query)`, which
triggers a refetch via the existing search/sort plumbing.

Participates in the tab's `alt+arrow` focus graph as the
`entry_search` node — see "Cross-region focus" below.

## Multi-select

The selection-set state machine lives on
`MultiSelectableViewModelMixin` (see
`widgets/browser/multi_selectable_table/CONTEXT.md`); the entries VM
mixes it in at the leaf and supplies the abstract surface
(`_selectable_items` → `self._entries`, `_item_id` → `e.id`, `cursor`
property already present) plus the `_on_selection_changed` hook
(pushes `_details.set_multi_select(...)` + re-syncs the linked-
flashcards panel target set). `_EntriesTable` subclasses
`MultiSelectableDataTable` to inherit the `space` /
`shift+up` / `shift+down` bindings.

The user toggles multi-select with `m` while the entries table is
focused; once on, `space` adds/removes the cursor row from the
selection set. Turning multi-select **off** abandons the selection
— there's no "preserve selection across mode flips" affordance.

Selections are keyed by entry id rather than row index so they survive
`load_more` calls and filter/search/sort-driven refetches. The view
adds a leading "sel" column (always present, width 3) and renders
`[ ]` / `[x]` markers only while multi-select is active; selected rows
render bright green (`#5fd75f`), and the rest of the table shifts to a
darker zebra palette via a `-multi-select` CSS class on the
`DataTable`. The status line is replaced by a "multi-select: N
entries selected" hint while the mode is on.

`toggle_multi_select` is overridden to drop relink mode on entry
(relink is single-select only); the override calls
`super().toggle_multi_select()` after the relink exit so the mixin
still owns the flag flip + selection clear + `_on_selection_changed`
push + `dirty` emit.

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
in `LINKED_FLASHCARDS`. Clicking the `<` nav arrow at the left edge of
the linked-flashcards panel exits all the way back to `ENTRIES` (see
"Right-panel tab navigation" below); `transition_to(ENTRIES)`
auto-exits relink as cleanup since the panel is no longer visible.

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
`_RelinkChoicesList(ChoiceList[LinkedFlashcardsPanelViewModel])`
mounted unconditionally in `compose`, with its visibility toggled
via the `.-visible` CSS class in `_refresh` based on
`vm.is_relink_dirty`. The base `ChoiceList` widget
(`widgets/browser/choices/`) owns the cursor, arrow nav, and the
standard `► Accept   Cancel` rendering; the subclass declares
`CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}` and
implements those two methods plus `action_cancel`. The cursor is
local widget state and resets to Accept via `prepare_for_show()` on
each show transition.

The choices widget is **not** in the tab's `alt+arrow` focus graph —
reach it via the `tab` key or click. The mode owns `←` / `→` for
cursor mutation, `enter` for dispatch, and `esc` as a Cancel shortcut.
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

## Right-panel tab navigation

The right-hand side of the tab body behaves as two "tabs" — entry
details (in `ENTRIES`) and the linked-flashcards table (in
`LINKED_FLASHCARDS`). The user cycles between them with the `tab`
key, bound on `KnowledgeEntryBrowserTabView` to
`action_tab_cycle_mode`, which two-state-toggles the VM via
`vm.transition_to(...)`. The `tab` binding is gated by the same
`_typing_active` check used by the other global tab keys so it
doesn't swallow keystrokes while an `Input` / `TextArea` is focused.
The keybinding is surfaced in the permanent `#tab-keybindings` hint
row at the bottom of the tab. `l` (relink) still transitively swaps
to `LINKED_FLASHCARDS` as a side effect of entering relink mode.

A vertical `Rule` (`#tab-body-rule`) sits between the entries column
and the right-hand panel area, and the entries column carries a
1-char `margin-right` so the rule doesn't sit flush against the
table.

## Keybindings line

A permanent 1-line `#tab-keybindings` Static docked at the very bottom
of the tab, left-aligned, listing the global mode/navigation keys so
they're discoverable without an explicit help toggle. A thin
`#tab-keybindings-rule` `Rule` sits above it in `#3a3a3a` as a visual
separator from whichever dialog (or status row) sits directly above.
Painted once in `compose` via `_keybindings_text` — pure static
content, no VM subscription. Each entry renders the key in a brighter
grey (`#a0a0a0`) than the action label (`#707070`) so the keybinding
pops out of the row. Per-dialog keys (← / → / enter / esc inside an
open dialog) are documented by the dialog widgets themselves.

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

Pressing `f` opens `_FilterDialog`. It's a `Vertical` container (not a
single `Static`, unlike the other dialogs) so it can embed a real
focusable Input next to the radio row. It surfaces two filter axes
stacked vertically:

- **Row 0 — `filter by type:`** (multi-select): a horizontal
  `[x] fact   [ ] exposition …` row backed by `vm.entry_types`.
  `None` means all types selected (no filter); a tuple restricts.
  `space` flips the cursor's option and calls `vm.set_type_filter`,
  collapsing back to `None` if every type ends up selected.
- **Row 1 — `filter by flashcards:`** (mutually exclusive radio):
  `(•) None   ( ) Any   ( ) No flashcards   ( ) One of: [____]`.
  The first three are backed by `vm.has_flashcards` (`None` / `True` /
  `False`); "One of" is backed by `vm.flashcard_ids` (`tuple[int, ...]
  | None`). The two VM axes are mutually exclusive — setting either
  wipes the other (see "Two-axis tagged union" below).

Navigation: `↑` / `↓` move the cursor between rows (column clamps to
the new row's length); `←` / `→` move within a row (wrap). On the
flashcards row, the cursor reaches a fourth column past the radios —
the "One of" pseudo-option. `r` clears **both** axes at once (resets
the dialog cursor to row 0 / col 0) and also clears the One-of buffer.
`f` / `escape` dismiss; `s` / `e` swap.

#### "One of" sub-flow

Selecting "One of" via `space` activates the radio bullet on the view
side (`_one_of_selected`), clears any prior VM flashcard filter
(`set_flashcard_filter(None)` — wipes both axes thanks to the mutual-
exclusion invariant), and focuses the compact `_OneOfInput` widget
mounted next to the label. Re-pressing `space` on "One of" while
already active just re-focuses the input (useful for resuming edits
after a submit).

Inside the input:

- `enter` parses the buffer via `_parse_id_list` (comma-separated
  tokens, stripped, `int()`-parsed; unparseable or empty tokens are
  silently dropped) and pushes the resulting tuple to
  `vm.set_flashcard_ids_filter`. An empty parsed result clears the
  VM-side filter (treated as "stop filtering" rather than
  "no rows") but the dialog stays in "One of" mode so the user can
  keep editing. The input's placeholder doubles as the format hint
  (`"comma-separated ids"`).
- `escape` clears the buffer, drops `_one_of_selected` back to False,
  calls `vm.set_flashcard_filter(None)` to wipe both VM axes, and
  returns focus to the dialog.

After enter or escape, focus returns to the dialog itself (so the
user can resume `↑/↓/←/→` navigation).

Switching to a different radio (`Any` / `No flashcards` / `None`)
via `space` calls `vm.set_flashcard_filter(value)`, which wipes the
`flashcard_ids` axis as a side effect. The view-side `_one_of_buffer`
is **preserved** so the user can return to it via a future
`space`-on-"One of" without retyping; only `_one_of_selected` flips
back to False.

#### Two-axis tagged union

The flashcards filter is two VM fields (`has_flashcards: bool | None`
and `flashcard_ids: tuple[int, ...] | None`) but presents as one
radio choice. Both setters (`set_flashcard_filter`,
`set_flashcard_ids_filter`) wipe the twin axis so they can never both
be active. The DB op treats them as orthogonal predicates — the VM
owns the mutual-exclusion invariant. The empty-tuple convention
matches `entry_types`: an empty `flashcard_ids` tuple is "no rows
match", and `None` is "no filter on this axis".

Both VM mutators (`set_type_filter`, `set_flashcard_filter`,
`set_flashcard_ids_filter`) clear the selection (same rationale as
the sort dialog — a different filter is a different `LIMIT 500`
window) and trigger a refetch via `_request_fetch`. The active
filters are projected onto the DB op's `entry_types` (list of
`EntryType` enums, or `None` for no filter; empty list = "no rows
match"), `has_flashcards` (`bool | None` — `True` ⇒ EXISTS on
`flashcard_entry`, `False` ⇒ NOT EXISTS, `None` skipped), and
`flashcard_ids` (iterable of ints, or `None`; empty iterable = "no
rows", non-empty = EXISTS + IN). Filter state lives entirely in the
VM and persists across dialog open/close cycles; the view-side One-of
buffer also persists across opens (the dialog is mounted once and
reused).

The widget is intentionally not generalized over an abstract
"category" list — the previous `FilterCategoryViewModel` /
`MultiSelectFilterViewModel` hierarchy was speculative generality
that never paid off. Adding a third filter axis (text-CONTAINS, date
range, etc.) follows the pattern used to add the flashcards row:
extend the dialog's render + keystroke dispatch, add a new VM
mutator + property, thread a new kwarg through `_query_kwargs` /
`_apply_entry_filters`.

### Sort

Pressing `s` opens `_EntriesSortDialog`, a thin specialisation of the
shared generic `SortDialog` (see
`widgets/browser/sort_dialog/CONTEXT.md` for the dialog's full
behaviour contract — cursor-and-arrow rendering, `enter` toggle
semantic, `r` reset, `escape` dismiss). The tab's VM mixes in
`SortableViewModelMixin[EntrySortKey]` and surfaces four axes via
`sort_options()` — `id`, `title`, `type`, `topic` — mirroring the
data table's column order left-to-right.

The DB op handles the two non-column axes: `type` uses a `CASE`
expression (locks the semantic order fact → exposition → overview
rather than the natural string sort which puts exposition first), and
`topic` joins onto the `Topic` table and orders on `lower(Topic.name)`
for case-insensitive alpha.

`vm.set_sort` clears the selection (a new sort means a new `LIMIT
500` window, and tracking selections across reshuffled windows is
more complexity than the feature warrants). `_EntriesSortDialog`
overrides `_extra_hint` to surface the "Applying clears your
selection." warning inline with the dialog's keybinding hint while
multi-select is on; the mode itself stays on, just with an empty set.

Sibling-dialog swap keys (`d` / `f` / `e`) are *not* bound on the
generic `SortDialog` — they bubble to the tab's BINDINGS, which owns
the dialog mutex. `s` likewise bubbles to the tab's `s`-toggle, which
closes the active sort dialog.

### Delete

Pressing `d` opens `_DeleteConfirm`, a thin
`ChoiceList[KnowledgeEntryBrowserTabViewModel]` subclass mounted
between the tab body and the docked status line. The base widget
(`widgets/browser/choices/`) owns cursor / arrow nav / standard
`► Confirm` rendering; the subclass declares
`CHOICES = {"Confirm": "_confirm", "Cancel": "_cancel"}`,
`ORIENTATION = "vertical"` (stacked), and overrides `_render_header`
to surface the count prose (`Delete N selected entries?` in
multi-select / `Delete 1 entry?` in single-select). Sibling-swap keys
(`s` / `f` / `e`) bubble to the tab's BINDINGS like every other
ChoiceList-based dialog.

On confirm the dialog awaits `vm.delete_selected_entries()` and then
hides itself. The VM resolves the target set via the internal
`selected_target_ids()` helper (the live selection set in
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

Pressing `e` opens `_EditBar`, a `ChoiceList` subclass with dynamic
choices and a custom per-choice renderer (no `►` marker — colour-only
distinction so the horizontal row stays compact across 3-5 options).
The option list lives on the view side as module-level constants:

- **multi-select** (`_EDIT_OPTIONS_MULTI`): `change topic` ·
  `change type` · `delete`
- **single-select** (`_EDIT_OPTIONS_SINGLE`): `change topic` ·
  `change type` · `edit title` · `edit content` · `delete`

`edit title` / `edit content` are excluded in multi-select because
the details panel is frozen and there's no single entry to refocus
onto. `delete` always sits last so the cursor never lands on the
destructive action without an explicit rightward step.

All labels map to a single `_dispatch` action method (via
`choices()` override returning `{label: "_dispatch"}` for every
label); `_dispatch` reads `self._cursor` to recover the selected
label and forwards it to `tab.handle_edit_choice(label)`. The tab
dispatches:

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

## Cross-region focus

`alt+←` / `alt+↑` / `alt+→` / `alt+↓` (driven by `BrowserView`'s
priority bindings) walk a directional graph over the tab's focusable
regions. The tab implements `nav_up` / `nav_down` / `nav_left` /
`nav_right`; each names the currently-focused node via `_focused_node`
and dispatches to a target via `_focus_node` (which gates on
`_node_present`). The walkers are intentionally written as explicit
`if`-chains — no transition table, no generalized graph object — so
edges are easy to read off the source and easy to change.

Nodes (each maps to one focusable widget id):

| Node                       | Widget id                       | Present when                                                  |
|----------------------------|---------------------------------|---------------------------------------------------------------|
| `entry_search`             | `search-input`                  | always                                                        |
| `entry_table`              | `entries-table`                 | always                                                        |
| `dialog`                   | the currently-shown dialog      | a dialog is open (`_active_dialog is not None`)               |
| `entry_title`              | `details-title`                 | `ENTRIES` state, not multi-select-frozen                      |
| `entry_content`            | `details-content`               | `ENTRIES` state, not multi-select-frozen                      |
| `entry_modification_accept`| `details-choices`               | `ENTRIES` state, not frozen, and `details.is_dirty`           |
| `flashcard_search`         | `linked-flashcards-search-input`| `LINKED_FLASHCARDS` state                                     |
| `flashcard_table`          | `linked-flashcards-table`       | `LINKED_FLASHCARDS` state                                     |
| `relink_choices`           | `linked-flashcards-relink-choices` | `LINKED_FLASHCARDS` state and `linked_flashcards.is_relink_dirty` |

The topic tree is a sibling region owned by `BrowserView`, not the tab.
`nav_left` returns the sentinel string `"topic_tree"` for edges that
escape the tab leftward; `BrowserView` catches that and focuses the
tree.

Edges (each `node → node` arrow is gated on the target node being
present; if absent, the keystroke is a silent no-op):

- **`alt+up`**:
  - `dialog → entry_table`
  - `entry_table → entry_search`
  - `flashcard_table → flashcard_search`
  - `relink_choices → flashcard_table`
  - `entry_modification_accept → entry_content`
  - `entry_content → entry_title`
  - `entry_title → entry_search`

- **`alt+down`**:
  - `entry_search → entry_table`
  - `flashcard_search → flashcard_table`
  - `entry_table → dialog`
  - `flashcard_table → relink_choices` (fall through to `dialog`
    when the relink panel isn't dirty)
  - `relink_choices → dialog`
  - `entry_title → entry_content`
  - `entry_content → entry_modification_accept` (fall through to
    `dialog` when accept is absent)
  - `entry_modification_accept → dialog`

- **`alt+left`**:
  - `entry_search → topic_tree` (sentinel)
  - `entry_table → topic_tree` (sentinel)
  - `dialog → topic_tree` (sentinel)
  - `entry_title → entry_search`
  - `entry_content → entry_table`
  - `entry_modification_accept → entry_table`
  - `flashcard_search → entry_search`
  - `flashcard_table → entry_table`
  - `relink_choices → entry_table`

- **`alt+right`**:
  - `entry_search → entry_title` (in `ENTRIES`) / `flashcard_search`
    (in `LINKED_FLASHCARDS`)
  - `entry_table → entry_content` (in `ENTRIES`) / `flashcard_table`
    (in `LINKED_FLASHCARDS`)

The relink Accept/Cancel widget participates as `relink_choices` while
the relink panel is dirty (analogous to `entry_modification_accept` on
the entries side). It owns its own `←`/`→`/`enter`/`esc` bindings for
cursor + action dispatch.
