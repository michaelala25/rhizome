# rhizome/tui/widgets/browser/knowledge_entry_tab/entry_details/

Title/content side panel mounted to the right of the entry table in `KnowledgeEntryBrowserTabView`.
Buffered edit with explicit Accept/Cancel.

## Files

- **view_model.py — `EntryDetailsViewModel`**: buffered-edit VM. Exposes the standard `dirty` group
  plus a `SAVED` group the tab VM subscribes to after a successful Accept. Full contract in the module
  docstring.
- **view.py — `EntryDetailsView`** (+ private `_ChoicesList`): `Vertical(title TextArea, content
  TextArea, hidden-when-clean Accept/Cancel)`. `_ChoicesList` extends the shared `ChoiceList` and wires
  Accept/Cancel through its `CHOICES` action-string dispatch.

## Invariants worth flagging

- **Cursor-move-while-dirty = silent discard.** The tab VM calls `set_entry` on every cursor move,
  which unconditionally reseeds the buffers. Users must Accept before moving on.
- **Choices visibility.** `_refresh` toggles the `.-visible` CSS class on `_ChoicesList` from
  `vm.is_dirty`. On each clean->dirty transition it calls `prepare_for_show()` so a fresh open lands
  on Accept.
- **Focus-orphan rescue.** On the dirty->clean transition, if focus was on the (about-to-hide)
  choices widget, `_refresh` re-routes focus to the content `TextArea` before hiding.
- **Multi-select freeze.** When the tab pushes `set_multi_select(active=True, ...)`, both `TextArea`s
  go `read_only=True` and the choices stay hidden. The cursor still moves entries through, so the
  panel keeps showing the current row.
- **Focus graph.** The three regions participate in the parent tab's `alt+arrow` graph as
  `entry_title` / `entry_content` / `entry_modification_accept`. They drop out of the graph while
  multi-select is active (see the parent tab's `CONTEXT.md`).
