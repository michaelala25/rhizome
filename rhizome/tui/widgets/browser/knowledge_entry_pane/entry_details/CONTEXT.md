# rhizome/tui/widgets/browser/knowledge_entry_pane/entry_details/

The title/content side panel that sits to the right of the entry table
inside `KnowledgeEntryBrowserPaneView`. Buffered-edit model with an
explicit Accept/Cancel choices list. See the parent
`browser/CONTEXT.md` for the full behaviour notes (dirty semantics,
cursor-move-while-dirty discard policy, `SAVED` callback group,
focus-orphan rescue, cross-region focus contract).

## Layout

- **view_model.py — `EntryDetailsViewModel`**: holds the current entry
  plus `_title_buffer` / `_content_buffer` seeded on `set_entry`.
  `is_dirty` is true when either buffer diverges from the stored value.
  Exposes the standard `dirty` group plus a dedicated `saved` group the
  pane VM subscribes to. `accept` opens a session, calls `update_entry`
  + commits, then mutates the in-memory `KnowledgeEntry` in place;
  `cancel` restores the buffers from the entry.

- **view.py — `EntryDetailsView` + private `_ChoicesList`**:
  `Vertical` containing a title `TextArea`, a content `TextArea`, and
  the hidden-when-clean `_ChoicesList`. `_ChoicesList` is a focusable
  `Static` with its own up/down/enter bindings that dispatch to
  `vm.move_choice_cursor` / `vm.accept` / `vm.cancel`. The view
  implements the pane sub-region focus contract (`focus_first`,
  `focus_next_region`, `focus_prev_region`) walking
  `_REGION_IDS = (title, content, choices)` and skipping hidden
  regions.
