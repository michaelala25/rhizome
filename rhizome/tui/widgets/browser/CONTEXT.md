# rhizome/tui/widgets/browser/

Multi-tab data browser backed by a multi-select topic tree. Two top-level regions: the
topic-tree panel on the left (filter source) and the tab bar + active tab on the right (filter
consumer). Designed to scale to ~100K entries × ~1K topics via windowed fetches and the shared
debounce/staleness kernel on `QueryBackedViewModel`. Wired into the chat tab as `/browse`; the
slash command appends a fresh `BrowserViewModel` to the feed and `BrowserView` builds against it.

## Files & subdirectories

- **view.py — `BrowserView`** — thin top-level `Horizontal`. Composes the panel + the tab bar
  over a `ContentSwitcher` of pre-mounted tab views. Owns the `Ctrl+←/→` tab cycle and dispatches
  the `alt+arrow` cross-region focus walk. See the module docstring for the dispatch contract.
- **view_model.py — `BrowserViewModel`** — orchestrator. Owns the panel VM and a fixed list of
  tab VMs; subscribes to `panel.filter_changed` and pushes the new filter into the active tab.
  Inactive tabs catch up lazily on switch via the idempotent `set_topic_filter`.
- **tab_base.py — `BrowserTabViewModel`** — abstract base every tab inherits from. Adds tab
  identity (`TITLE`) and `set_topic_filter` on top of `QueryBackedViewModel`'s fetch kernel.
- **topic_tree.py — `BrowserTopicTreeViewModel` + `BrowserTopicTreeView`** — the multi-select
  tree itself. Selection is cascade-on-toggle; `_selected_ids` already holds the expanded filter
  set, so `expanded_filter_ids()` is a sync read. See the module docstring for the View/VM split.
- **topic_summary.py — `TopicSummaryViewModel` + `TopicSummaryView`** — read-only summary panel
  for the cursor-highlighted topic. Inherits `QueryBackedViewModel` so fast cursor scrolls
  collapse into one eventual query.
- **topic_tree_panel/** — bundles the action menu + tree + summary as a single rail, behind one
  panel VM + view that `BrowserView` treats as one region. See its CONTEXT.md.
- **knowledge_entry_tab/** — the first concrete `BrowserTabViewModel`: paginated `DataTable` of
  knowledge entries with editable details and a swappable linked-flashcards panel. See its
  CONTEXT.md.
- **choices/** — `ChoiceList`, the shared base for browser-tab dialogs that present a navigable
  list of named choices (Accept/Cancel, Confirm/Cancel, edit picker, relink confirm). See its
  CONTEXT.md.
- **multi_selectable_table/** — `MultiSelectableDataTable` + `MultiSelectableViewModelMixin`,
  the shared multi-select scaffolding for browser-tab tables. See its CONTEXT.md.
- **sort_dialog/** — `SortDialog` + `SortableViewModelMixin`, the shared sort-axis picker. See
  its CONTEXT.md.

## Conventions

- **View ↔ VM**. Each view subscribes its sync `_refresh` to `vm.dirty` in `on_mount` and
  unsubscribes in `on_unmount`. View → VM is always a direct call from an event handler; VM → VM
  is also direct (the orchestrator subscribes to the panel; the panel subscribes to its children).
  No DB I/O in views.
- **Filter semantics — `None` vs empty.** `None` = "no filter, show everything"; an empty
  iterable = "selection is non-empty in principle but expanded to zero topics". Both are legal
  terminal states preserved end-to-end (orchestrator, tab base, DB ops).
- **Cross-region focus is view-side.** No VM knows or cares which sub-region is focused.
  `BrowserView` dispatches `alt+arrow` to `panel.nav_*` / `tab.nav_*` (priority bindings so they
  fire even when a `TextArea` is focused); each side resolves one step in its own focus graph and
  returns a bool, or — for tabs — the sentinel string `"topic_tree"` to ask `BrowserView` to land
  focus back on the tree. See `view.py` for the dispatcher and `knowledge_entry_tab/view.py` for
  the tab's node/edge graph.
- **Panel filter contract is panel-shaped.** The orchestrator subscribes to
  `panel.filter_changed` and reads `panel.current_filter`. It never reaches into `panel.tree`.
