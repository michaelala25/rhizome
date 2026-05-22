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
  `_has_more`, search/sort state, the row cursor, and a child
  `EntryDetailsViewModel` exposed via `self.details`. Also exports
  `DEFAULT_PAGE_LIMIT = 500`. See the parent `CONTEXT.md` for the
  detailed behaviour notes (windowed fetch + count, cursor-doesn't-emit-
  dirty, `_on_details_saved` repaint, etc.).

- **view.py — `KnowledgeEntryBrowserPaneView`**: `Vertical` containing a
  `Horizontal #pane-body` (table at 60%, `EntryDetailsView` at 40%) and a
  docked one-line status row. Implements the pane-view focus contract
  (`focus_first` / `focus_next_region` / `focus_prev_region`) and
  delegates the details region's internal cycle to `EntryDetailsView`.

- **entry_details/ — `EntryDetailsView` + `EntryDetailsViewModel`**:
  buffered-edit side panel. Its own MVVM subdirectory; see its
  `CONTEXT.md`.
