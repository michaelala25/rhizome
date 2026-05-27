# rhizome/tui/widgets/browser/knowledge_entry_tab/

First concrete `BrowserTabViewModel`: paginated `DataTable` of `KnowledgeEntry` rows + a
swappable right-hand panel (editable details in `ENTRIES`, linked-flashcards table in
`LINKED_FLASHCARDS`) + four bottom dialogs (delete / sort / filter / edit).

Read this with the parent `browser/CONTEXT.md` (orchestrator + tab base + cross-region focus
contract) and `docs/design-principles.md` (MVVM conventions).

## Files

- **view_model.py — `KnowledgeEntryBrowserTabViewModel`**: subclasses `BrowserTabViewModel`;
  mixes in `MultiSelectableViewModelMixin`, `SearchableViewModelMixin`,
  `SortableViewModelMixin[EntrySortKey]`. Owns the windowed entries list (capped at
  `DEFAULT_PAGE_LIMIT = 500`), `_total`, `_has_more`, search/sort/type/flashcard filter state,
  row cursor, and child VMs (`details: EntryDetailsViewModel`,
  `linked_flashcards: LinkedFlashcardsPanelViewModel`). Bulk-action surface:
  `set_sort`, `set_type_filter`, `set_flashcard_filter`, `set_flashcard_ids_filter`,
  `delete_selected_entries`, `change_topic_on_selected_entries`,
  `change_type_on_selected_entries`. See the module docstring for the fetch / cursor / refetch
  contract.

- **view.py — `KnowledgeEntryBrowserTabView`**: `Vertical` with `Horizontal #tab-body` over a
  docked status row and keybindings line. Body is a left `#table-column` (search input over the
  `_EntriesTable`) and a right pane that swaps `EntryDetailsView` ↔ `LinkedFlashcardsPanelView`
  via the `-state-*` CSS class. Owns the dialog mutex (`_active_dialog` +
  `show_dialog`/`hide_dialog`/`toggle_dialog`), the global tab keys
  (`d`/`s`/`f`/`e`/`l`/`m`/`tab`, gated on `_typing_active`), `focus_first`, and the
  cross-region focus graph. See the module docstring for the focus graph table and the dialog
  mutex contract.

- **delete_dialog.py — `_DeleteConfirm`**: vertical Confirm/Cancel `ChoiceList`. Confirm
  awaits `vm.delete_selected_entries()` then hides; cancel just hides. Header shows the target
  count + a "linked flashcards untouched" note.

- **edit_dialog.py — `_EditBar` + `_TypePickerScreen`**: horizontal `ChoiceList` whose options
  depend on multi-select (`_EDIT_OPTIONS_MULTI` vs `_EDIT_OPTIONS_SINGLE`); every choice
  dispatches through `tab.handle_edit_choice(label)`. `change topic` / `change type` push modal
  pickers (`TopicSelectorScreen` / `_TypePickerScreen`) and apply on dismiss; `edit title` /
  `edit content` focus the corresponding details TextArea; `delete` swaps to the delete dialog.

- **filter_dialog.py — `_FilterDialog` + `_OneOfInput`**: two-axis filter — multi-select type
  row over a mutually-exclusive flashcard radio row (`None` / `Any` / `No flashcards` /
  `One of: [____]`). See the module docstring for the cursor model, the "One of" sub-flow, and
  the tagged-union mutual exclusion the VM enforces between `has_flashcards` and
  `flashcard_ids`.

- **entry_content_preview.py — `_EntryContentPreview`**: read-only `TextArea` showing the
  cursor entry's content. Visible only in `LINKED_FLASHCARDS` (the details panel covers the
  same job in `ENTRIES`). Non-focusable.

- **entry_details/** — buffered-edit title/content panel + its accept/cancel choices widget.
  Driven via `vm.details`. See its `CONTEXT.md`.

- **linked_flashcards/** — right-hand flashcards panel + the relink dual-section / pool fetch
  state machine. Driven via `vm.linked_flashcards`. See its `CONTEXT.md`.

## Conventions

- VM owns data facts; the view owns dialog UI state (cursor, which dialog is open). Dialog
  widgets keep their own local cursor + expose `prepare_for_show()`.
- Dialog mutex lives view-side. State transitions auto-dismiss any open dialog.
- Multi-select gates: `m` toggles; entering relink drops multi-select (single-select only);
  toggling `m` on while in relink exits relink first.
- `tab` cycles the right-pane state (`ENTRIES` ↔ `LINKED_FLASHCARDS`); `l` transitively swaps
  to `LINKED_FLASHCARDS` as a side effect of entering relink.
- Any sort/filter mutator clears the selection — a fresh `LIMIT 500` window invalidates the
  position-based meaning of the selection set. After topic/type bulk edits, selections survive
  via `_post_change_refetch` only for entries still in the new window.
- `_search` / `entry_types` / `has_flashcards` / `flashcard_ids` all participate in
  `_query_kwargs` and the `_apply_entry_filters` DB helper. `None` = no filter; empty tuple =
  "no rows match" (legal terminal state).
- `set_cursor` deliberately does **not** emit `dirty` — the table rebuild during `_refresh`
  feedback-loops with `DataTable.RowHighlighted` otherwise. Cursor moves are visible via the
  table's own render + the detail panel's separate `dirty`.
