# rhizome/tui/widgets/browser/

The replacement for the legacy `ExplorerViewer`. A multi-tab data browser
backed by a multi-select topic tree, designed to scale to ~100K knowledge
entries × ~1K topics via windowed fetches and cancellable refreshes. See
`braindump.md` for the long-form design rationale and `docs/design-principles.md`
for the MVVM conventions this module follows.

## Status

Minimal end-to-end: VMs + views in place, no CSS theming or polish, no
detail panel, no search/sort UI. Wired into the MVVM chat tab as
`/browse` — the slash command appends a fresh `BrowserViewModel` to the
feed, which the tab view dispatches to a `BrowserView(vm)`. Standalone
callers construct the VM directly: `BrowserView(BrowserViewModel(session_factory))`.
Iteration on the VM contracts continues; layout and additional UI
features land as separate passes.

## View ↔ VM coordination convention

Every view in this module follows the same shape:

  * The view subscribes its `_refresh` to `vm.dirty` in `on_mount` and
    unsubscribes in `on_unmount`. Subscription is *not* done in `__init__`
    because `_refresh` reads widget DOM via `query_one`.
  * `_refresh` is sync (callbacks are always sync) and reads VM state
    directly — no DB calls in the view.
  * View → VM is always a direct method call from an event handler
    (`on_tree_node_expanded → await vm.expand(id)`, etc.).
  * VM → VM coordination is direct method calls between VMs; views never
    talk to each other.

This matches `docs/design-principles.md` and the established
`flashcard_review/` pattern.

## Components

- **view.py — `BrowserView`**: deliberately thin top-level Horizontal
  widget. Composes the topic-tree panel on the left (one widget — see
  `topic_tree_panel/`) and the tab bar + `ContentSwitcher` on the right;
  every tab view is mounted up front so switching tabs is just a
  `switcher.current` flip. `Ctrl+Left/Right` cycle tabs via
  `vm.prev_tab` / `vm.next_tab`. `on_mount` calls `await self._vm.start()`
  after child widgets have subscribed, then focuses the tree (via
  `panel.focus_tree()`). Fixed `height: 40` because `1fr` collapses to 0
  inside the chat tab's `VerticalScroll` feed (no remaining space to claim
  when the container derives its size from children).
  `BrowserView.focus()` is overridden to route to the tree inside the
  panel (Horizontal isn't focusable), so `vm.request_focus()` from chat-tab
  feed nav lands on the tree.

  Cross-region focus is dispatched against two top-level regions — the
  panel and the active tab. The view doesn't reach inside either: it
  calls `panel.nav_left/nav_right/focus_tree` and `tab.nav_left/nav_right/
  nav_up/nav_down/focus_first` and reacts to their return values (see the
  "Cross-region focus navigation" section below for the full contract).

  Tab-VM → tab-view mapping lives in the private `_view_for_tab`
  dispatch in this file — `isinstance` against the concrete VM class.
  Single concrete tab for now (`KnowledgeEntryBrowserTabViewModel` →
  `KnowledgeEntryBrowserTabView`); when we add more, extend the dispatch
  or move to a per-VM `make_view()` factory.

- **view_model.py — `BrowserViewModel`**: deliberately thin top-level
  orchestrator. Sets `is_navigable = True` so the chat tab's
  `ctrl+up`/`ctrl+down` feed nav can land on the browser. Owns the
  topic-tree panel VM (which in turn owns the tree, actions menu, and
  summary — see `topic_tree_panel/`) and a fixed-at-construction list of
  tab VMs (pass `tabs=None` for the production default from
  `_default_tabs`, or pass a list in tests to override). Subscribes to
  `panel.filter_changed` and hands `panel.current_filter` straight to the
  **active** tab — inactive tabs catch up lazily on `switch_tab` via the
  idempotent `set_topic_filter`. `next_tab`/`prev_tab` cycle with
  wrap-around. Single `dirty` group fires on `switch_tab`. Call `await
  start()` once after mounting to seed the active tab with the panel's
  current (empty) filter.

  The orchestrator never reaches into `panel.tree` / `panel.summary` etc.
  — the panel is its only neighbour on the left, the tabs its only
  neighbours on the right.

- **topic_tree_panel/** — the left rail bundled as a single panel
  (view + VM + the panel-internal `topic_tree_actions.py` + its own
  `CONTEXT.md`). Owns the action menu, topic tree, and topic summary
  plus their internal wiring (cursor → summary; selection → re-emit as
  `filter_changed`). Exposes a small contract to the orchestrator
  (`filter_changed`, `current_filter`) and to `BrowserView` (`nav_left`,
  `nav_right`, `focus_tree`). All rail CSS — width, expansion-on-focus,
  vertical rule between actions and tree, summary border — lives on the
  panel view.

- **topic_summary.py — `TopicSummaryViewModel` + `TopicSummaryView`**:
  read-only summary panel for the cursor-highlighted topic, mounted by
  the topic-tree panel below the body row. Shows name + id + description
  plus direct/subtree counts of knowledge entries and flashcards. The VM
  is a `QueryBackedViewModel`, so fast cursor scrolling collapses into a
  single eventual fetch via the standard debounce. Driven by the panel
  VM, which subscribes to `tree.cursor_changed` and calls
  `summary.set_topic_id(tree.cursor_topic_id)`; `set_topic_id` is
  idempotent and bypasses the debounce path for `None` (synchronous
  clear). Subtree counts use `expand_subtrees` +
  `count_entries_filtered` / `count_flashcards_by_topics`.


- **topic_tree.py — `BrowserTopicTreeViewModel` + `BrowserTopicTreeView`**:
  multi-select topic tree. **VM owns** selection (`_selected_ids`),
  cursor id (`_cursor_topic_id` — authoritative external reference; widget
  cursor mirrors it), and the DB-facing operations:
    * `async fetch_children(parent_id)` — `None` for roots, returns
      `list[LoadedTopic]` (each `Topic` paired with a `has_children` hint
      computed in batch via `find_parent_topic_ids`). Stateless: results
      flow through the return value, not VM state.
    * `async toggle_selection(topic_id)` — cascade-on-toggle. Awaits
      `expand_subtrees([topic_id])`, then either adds the whole subtree
      to `_selected_ids` (if any descendant was missing) or removes the
      whole subtree (if it was already fully covered). Tri-state
      "partial" (cascade-selected then descendant explicitly unchecked)
      counts as not-fully-selected, so a subsequent toggle re-adds
      everything — standard file-picker tri-state.
    * `expanded_filter_ids()` — **sync**. Returns `frozenset(_selected_ids)`
      or `None` for the empty-selection (no-filter) state. With
      cascade-on-toggle, `_selected_ids` is already the full subtree set,
      so no second-stage CTE expansion is needed at filter-propagation
      time.

  Selection is multi-set with subtree cascade. The VM emits a dedicated
  `SELECTION_CHANGED` callback group for the orchestrator on top of
  standard `dirty`. Both fire exactly once per toggle even though the
  cascade may add or remove many ids. A parallel `CURSOR_CHANGED` group
  fires on actual cursor-id changes (alongside `dirty`); consumers like
  the topic-summary panel listen to it so they don't refetch on every
  selection-toggle label repaint.

  **View owns** the visual tree structure entirely. It subclasses Textual's
  `Tree[Topic]` to inherit navigation, scrolling, and expand/collapse;
  the `TreeNode` tree IS the cache of loaded children — there is no
  parallel VM-side `_children` dict. On mount the view calls
  `vm.fetch_children(None)` and populates root TreeNodes. On
  `NodeExpanded` (first time per node), it calls `vm.fetch_children(id)`
  and stuffs the result into `node.children`; subsequent expansions of
  the same node skip the fetch because `node.children` is already
  populated. Multi-select checkboxes are drawn in a `render_label`
  override against `vm.is_selected`. View → VM in event handlers:
  `NodeHighlighted → vm.set_cursor(id)`, `Space → vm.toggle_selection(id)`.
  Enter is suppressed so it doesn't post a misleading `NodeSelected` up
  the DOM. VM → view via `dirty` triggers `_invalidate_label_cache`
  (bumps Textual's internal `_updates` counter so labels re-render
  against the new selection / cursor state). No structural sync happens
  through `dirty` — structural changes always come from user events.

- **choices/ — `ChoiceList`** (see the subdir's own `CONTEXT.md`):
  shared base for browser-tab dialogs that present a navigable list
  of named choices (Accept/Cancel, Confirm/Cancel, edit picker,
  relink confirm). Owns cursor + arrow nav + enter/escape + focus-
  brightness rendering. Subclasses declare `CHOICES: dict[str, str]`
  (label → action method name, mirroring Textual's `BINDINGS`
  action-string convention) plus optional `LEAD` / `HINT` / `_render_header`
  / `_render_choice` overrides. No VM mixin — action methods vary
  too much across consumers to justify a centralized contract.
  Sibling-dialog swap keys (`d` / `f` / `e` / `s`) bubble to the
  parent tab's BINDINGS like with `SortDialog`.

- **sort_dialog/ — `SortDialog` + `SortableViewModelMixin`** (see the
  subdir's own `CONTEXT.md`): shared sort-axis picker for browser
  tabs. Generic on the VM (bound to `SortableViewModelMixin`) and on
  the sort-key type. Concrete tabs mix in
  `SortableViewModelMixin[ConcreteSortKey]` at the leaf and implement
  `sort_options()` / `set_sort()` / `sort_by` / `sort_dir`; when they
  need a state-driven inline warning beyond the keybinding hints,
  they subclass `SortDialog` and override `_extra_hint`. Sibling-
  dialog swap keys (`d` / `f` / `e` / `s`) deliberately bubble to the
  parent tab's BINDINGS so the dialog stays decoupled from any
  specific tab's siblings.

- **tab_base.py — `BrowserTabViewModel(QueryBackedViewModel)`** (abstract):
  the tab contract. Thin layer on top of `QueryBackedViewModel` (see
  `widgets/CONTEXT.md`) that adds tab identity (`TITLE`, `title` property)
  and the orchestrator-facing topic-filter API. The debounce + fetch-id
  staleness machinery lives on the base; subclasses override `_fetch` /
  `_process_fetched_data` and call `_request_fetch` from their own
  sort/search mutators to trigger a refresh. `set_topic_filter(topic_ids)`
  (sync, called by the orchestrator) is **idempotent**: no-op if the
  requested filter equals the tab's current `_filter_ids` AND
  `_filter_applied` is True (i.e. the tab has been set up at least once);
  otherwise it bumps the fetch id and (re)schedules a debounced fetch.
  `topic_ids=None` is "no filter" — distinct from an empty iterable,
  which means "selection expanded to zero rows".

  Concrete tabs may *also* propagate `set_topic_filter` to sub-VMs they
  own (e.g. `KnowledgeEntryBrowserTabViewModel` overrides it to push the
  filter down into the linked-flashcards panel sub-VM before calling
  `super()`). Sub-VMs in the panel hierarchy inherit from
  `QueryBackedViewModel` directly — they share the fetch protocol but
  aren't tabs.

- **knowledge_entry_tab/ — `KnowledgeEntryBrowserTabViewModel` +
  `KnowledgeEntryBrowserTabView`** (split across `view_model.py` and
  `view.py`; see the subdir's own `CONTEXT.md` for the layout): the first
  concrete tab. VM window
  size is capped at `DEFAULT_PAGE_LIMIT` (500), with `load_more` appending
  the next page. `_fetch` runs the windowed SELECT first (so rows can
  paint before the total lands), then a separate COUNT for the "showing N
  of M" hint, with `_has_more` reconciled against the authoritative count.
  `set_search` / `set_sort` reset the window and cursor; `load_more`
  extends in place. `_cursor` is a window-local index (not an entry id),
  so it points at the same row before and after a `load_more`.

  The tab VM owns a child `EntryDetailsViewModel` (see below) exposed via
  the `details` property. `_sync_details()` pushes the cursor's entry
  (or `None`) into it; it's called from `set_cursor` and at the end of
  `_fetch`. Critically, `set_cursor` does **not** emit `dirty` itself —
  the tab view's `_refresh` rebuilds the `DataTable` in full, and
  rebuilding while the cursor is mid-move would feedback-loop with
  `DataTable.RowHighlighted`. Cursor moves are visible via the table's
  own cursor render and via the detail panel's separate dirty.

  The view is a `Vertical` containing a `Horizontal #tab-body` (table on
  the left at 60%, `EntryDetailsView` on the right at 40%) and a one-line
  `Static` status row docked to the bottom showing "loading…" / "showing
  N of M" / "N entries" / "no entries" based on VM state. Full table
  rebuild on every tab-VM `dirty`; after the rebuild the view calls
  `table.move_cursor(row=vm.cursor)` to restore the cursor position
  (`DataTable.clear()` resets it to row 0). `RowHighlighted` round-trips
  back through `vm.set_cursor`; the VM's equality early-return prevents
  loops. **No search bar yet** — deliberately benched for now; the seam
  for adding it is `set_search` on the VM and a `_search-bar` Static
  above the body.

- **knowledge_entry_tab/entry_details/ — `EntryDetailsViewModel` +
  `EntryDetailsView`** (split across `view_model.py` and `view.py`):
  the title/content panel to the right of the entry table.
  **Buffered-edit model.** The VM holds per-field buffers
  (`_title_buffer`, `_content_buffer`) seeded from the entry on
  `set_entry`; `title` / `content` return the buffers (not the entry's
  stored values), and `is_dirty` is true when either buffer diverges.
  `set_title` / `set_content` update the buffer with no DB side effect.

  Cursor-move-while-dirty: **silent discard**. `set_entry` unconditionally
  reseeds buffers from the new entry. The user has to explicitly Accept
  before navigating away.

  **Choices list.** When `is_dirty` is true, a `_ChoicesList`
  (`ChoiceList[EntryDetailsViewModel]` subclass with `LEAD = "Edit: "`
  and `CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}`) reveals
  below the content area. The base `ChoiceList`
  (`widgets/browser/choices/`) owns the cursor, arrow nav, and the
  standard `► Label` rendering; the subclass just wires the two
  action methods and `action_cancel`. The parent view's `_refresh`
  calls `prepare_for_show()` on the clean→dirty transition so each
  fresh open lands on Accept. When clean, the widget is hidden via a
  `.-visible` class toggle.

  **Accept path**: opens a session, calls `update_entry` + `commit`,
  then mutates the in-memory `KnowledgeEntry` instance in place so the
  tab VM's `self._entries[cursor]` reference picks up the new values
  without a refetch. Emits both `dirty` and a dedicated `SAVED`
  callback group; the tab VM subscribes to `SAVED` and emits its own
  `dirty` so the `DataTable` row repaints with the new title.

  **Cancel path**: restores the buffers to the entry's stored values
  and emits `dirty`. Choices list disappears on the next refresh.

  The VM has no subscriptions of its own. The tab VM is the only writer
  for `set_entry`; the view drives the buffer mutators and the choices
  list drives accept/cancel.

  **Widget choice**: both title and content are `TextArea`s with
  `soft_wrap=True`, so long titles wrap rather than overflowing
  horizontally. (An earlier iteration used `Input` for the title; that
  required a stale-event filter against `Input.Changed`'s snapshotted
  `value` field, since async dispatch could deliver outdated snapshots
  after rapid cursor scrolling overwrote the widget. `TextArea.Changed`
  carries no snapshot — the handler reads `text_area.text` live every
  time — so dropping `Input` removed both the filter and a whole class
  of latent edit-loss bugs.) Both fields render with dim grey borders
  (`#3a3a3a`) that brighten to `$accent` on focus.

## DB-side support

These ops live in `rhizome/db/operations/` (added for the browser, but
available to any caller):

- `topics.expand_subtrees(root_ids, *, max_depth=10) -> set[int]`: union of
  subtrees for a multi-root selection. Single recursive CTE, ID-only result.
- `topics.find_parent_topic_ids(candidate_ids) -> set[int]`: which of the
  given topic ids have at least one direct child. Batched lookup for the
  tree's expand-affordance hints.
- `entries.list_entries_paginated(...)` and `entries.count_entries_filtered(...)`:
  the window + count pair powering the knowledge-entries tab. Share an
  internal `_apply_entry_filters` helper so the count matches the window
  exactly (same topic-id and search semantics).

## Cross-region focus navigation (alt+arrow)

This is a **view-side concern** — no VM knows or cares which sub-region
is focused. `BrowserView` owns two top-level regions (the topic-tree
panel on the left, the active tab on the right) and delegates everything
*inside* each to a small `nav_*` / `focus_*` surface. Each side resolves
a single step in its own focus graph; `BrowserView` interprets the
return value to decide whether to advance to the next top-level region.

`BrowserView` binds `alt+left` / `alt+right` / `alt+up` / `alt+down`
with `priority=True` (so they fire even when a `TextArea` inside the
details panel is focused — otherwise the TextArea's own word-nav and
paragraph-jump bindings would swallow them). Dispatch by `screen.focused`
location:

- focus in panel, `alt+left` → `panel.nav_left()`; the panel is the
  leftmost top-level region, so `False` is a hard no-op.
- focus in panel, `alt+right` → `panel.nav_right()`; `False` (focus was
  already in the tree, the panel's rightmost sub-region) means "advance
  into the active tab" — `BrowserView` calls `tab.focus_first()`.
- focus in panel, `alt+up` / `alt+down` → no-op (no focusable region
  above or below the panel body).
- focus elsewhere, `alt+<dir>` → `tab.nav_<dir>()`. For `nav_left`, the
  return value `"topic_tree"` asks `BrowserView` to focus the tree
  inside the panel (via `panel.focus_tree()`); every other return is a
  `bool` indicating whether the focus moved (informational — `BrowserView`
  doesn't react to it directly).

**Panel surface** (called by `BrowserView`):

```python
def focus_tree(self) -> None:          # focus the topic tree specifically
def nav_left(self) -> bool:            # tree → actions; False at actions edge
def nav_right(self) -> bool:           # actions → tree; False at tree edge
```

**Tab-view interface** (convention; no formal Protocol yet — duck-typed
via `hasattr` until we have a second tab to share the contract with):

```python
def focus_first(self) -> None:        # entry point from tree (alt+right)
def nav_up(self) -> bool:             # single step up in the focus graph
def nav_down(self) -> bool:           # single step down
def nav_left(self) -> bool | str:     # bool, or "topic_tree" sentinel
def nav_right(self) -> bool:          # single step right
```

`KnowledgeEntryBrowserTabView` resolves each step against a named-node
graph spanning the entries-side widgets (search / table / title /
content / modification-accept), the dialog slot, and the linked-
flashcards-side widgets (search / table). Edges are gated on
*presence* — the details panel's title/content/accept nodes are absent
in `LINKED_FLASHCARDS` and when multi-select freezes the panel; the
`flashcard_*` nodes are absent in `ENTRIES`; the dialog node is absent
when no dialog is open. Transitions to an absent target silently
no-op. See `knowledge_entry_tab/CONTEXT.md` for the full edge list.

**Focus-orphan rescue**: when Accept/Cancel lands and the choices widget
hides, `EntryDetailsView._refresh` checks for the dirty→clean transition
and — if focus was on the choices widget — re-routes focus to the
content TextArea before removing the `-visible` class. Without this,
Textual leaves `screen.focused` on a `display: none` widget and the next
keystroke goes nowhere visible.

## Filter semantics (cross-component contract)

`None` and "empty iterable" are intentionally distinct throughout this
module:

- **`None`** = "no filter, show everything" — the boot state, and the state
  after clearing the tree selection.
- **empty set/iterable** = "selection is non-empty in principle but expanded
  to zero topics" — a legal terminal state that returns zero rows.

Both `BrowserTabViewModel.set_topic_filter` and `list_entries_paginated` honor
this distinction. `TopicTreePanelViewModel.current_filter` and the tab's
`_filter_ids` are both `frozenset[int] | None` to make the type echo the
semantics.
