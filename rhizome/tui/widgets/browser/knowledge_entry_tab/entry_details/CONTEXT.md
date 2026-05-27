# rhizome/tui/widgets/browser/knowledge_entry_tab/entry_details/

The title/content side panel that sits to the right of the entry table
inside `KnowledgeEntryBrowserTabView`. Buffered-edit model with an
explicit Accept/Cancel choices list. See the parent
`browser/CONTEXT.md` for the full behaviour notes (dirty semantics,
cursor-move-while-dirty discard policy, `SAVED` callback group,
focus-orphan rescue, cross-region focus contract).

## Layout

- **view_model.py — `EntryDetailsViewModel`**: holds the current entry
  plus `_title_buffer` / `_content_buffer` seeded on `set_entry`.
  `is_dirty` is true when either buffer diverges from the stored value.
  Exposes the standard `dirty` group plus a dedicated `saved` group the
  tab VM subscribes to. `accept` opens a session, calls `update_entry`
  + commits, then mutates the in-memory `KnowledgeEntry` in place;
  `cancel` restores the buffers from the entry. Also carries a
  `_multi_select_active` / `_multi_select_count` pair the tab VM
  pushes via `set_multi_select(active, count)` whenever it enters or
  leaves multi-select mode (or the selection set grows/shrinks); the
  view uses it to freeze edits.

- **view.py — `EntryDetailsView` + private `_ChoicesList`**:
  `Vertical` containing a title `TextArea`, a content `TextArea`, and
  the hidden-when-clean `_ChoicesList`. `_ChoicesList` is a focusable
  `Static` rendered as a lead-in "Edit:" label, a horizontal
  Accept / Cancel row, and a dim hint line — mirrors the relink
  Accept/Cancel widget on the linked-flashcards panel. The cursor
  (0 = Accept, 1 = Cancel) lives on the widget, not the VM —
  dialog UI state is a view concern; the VM exposes only the data
  actions (`accept`, `cancel`). Bindings: left/right (mutate
  `_choice_cursor` locally), enter (dispatches `vm.accept` /
  `vm.cancel` by `_choice_cursor`), and escape (shortcut for
  cancel). A `prepare_for_show()` hook is called by the parent
  view's `_refresh` on the clean→dirty transition so each fresh
  open lands on Accept. The three regions (title / content / choices)
  participate in the parent tab's `alt+arrow` focus graph as the
  named nodes `entry_title` / `entry_content` /
  `entry_modification_accept` — see the parent tab's `CONTEXT.md`
  ("Cross-region focus"). When the VM reports `multi_select_active`,
  the two `TextArea`s are switched to `read_only=True` and the choices
  list is kept hidden — the entry's title/content remain visible (the
  tab VM still pushes `set_entry` on cursor moves) but the user can't
  make changes until multi-select is turned off. The three details
  nodes are also excluded from the parent's focus graph while frozen,
  so `alt+arrow` skips past them.
