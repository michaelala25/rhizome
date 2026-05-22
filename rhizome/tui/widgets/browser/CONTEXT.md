# rhizome/tui/widgets/browser/

The replacement for the legacy `ExplorerViewer`. A multi-pane data browser
backed by a multi-select topic tree, designed to scale to ~100K knowledge
entries × ~1K topics via windowed fetches and cancellable refreshes. See
`braindump.md` for the long-form design rationale and `docs/design-principles.md`
for the MVVM conventions this module follows.

## Status

Minimal end-to-end: VMs + views in place, no CSS theming or polish, no
detail panel, no search/sort UI. Wired into the MVVM chat pane as
`/browse` — the slash command appends a fresh `BrowserViewModel` to the
feed, which the pane view dispatches to a `BrowserView(vm)`. Standalone
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

- **view.py — `BrowserView`**: top-level Horizontal widget. Takes an
  externally-constructed `BrowserViewModel` (caller-owned, matching the
  chat-pane MVVM convention used by `AgentMessageView`, etc.). Layout:
  topic tree on the left (25%) inside a bordered pane, tab bar + active
  pane on the right (75%). Pane visibility is delegated to a Textual
  `ContentSwitcher` — every pane view is mounted up front, switching tabs
  flips `switcher.current`. `Ctrl+Left/Right` cycle panes via
  `vm.prev_pane` / `vm.next_pane`. `on_mount` calls `await self._vm.start()`
  after child widgets have subscribed, then focuses the tree. Fixed
  `height: 30` because `1fr` collapses to 0 inside the chat pane's
  `VerticalScroll` feed (no remaining space to claim when the container
  derives its size from children). Borders use `$foreground-muted` for a
  dim grey outline. `BrowserView.focus()` is overridden to route to the
  tree (Horizontal isn't focusable), so `vm.request_focus()` from chat-pane
  feed nav lands on the tree.

  Pane-VM → pane-view mapping lives in the private `_view_for_pane`
  dispatch in this file — `isinstance` against the concrete VM class.
  Single concrete pane for now (`KnowledgeEntryBrowserPaneViewModel` →
  `KnowledgeEntryBrowserPaneView`); when we add more, extend the dispatch
  or move to a per-VM `make_view()` factory.

- **view_model.py — `BrowserViewModel`**: top-level orchestrator. Sets
  `is_navigable = True` so the chat pane's `ctrl+up`/`ctrl+down` feed nav
  can land on the browser. Owns the
  tree VM and a fixed-at-construction list of pane VMs (the tab bar — pass
  `panes=None` for the production default from `_default_panes`, or pass a
  list in tests to override). Subscribes to the tree's `SELECTION_CHANGED`
  and hands `tree.expanded_filter_ids()` straight to the **active** pane —
  the recursive-CTE expansion now lives inside `toggle_selection` itself
  (cascade-on-toggle), so the orchestrator's selection handler is a sync
  read with no background task or cancellation dance. Inactive panes
  catch up lazily on `switch_pane`. `next_pane`/`prev_pane` cycle with
  wrap-around. Single `dirty` group fires on `switch_pane`. Call `await
  start()` once after mounting to seed the active pane with the empty
  filter. (The tree view loads its own roots on mount; the VM doesn't
  cache tree shape, so there's nothing for the orchestrator to await.)

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
  cascade may add or remove many ids.

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

- **pane_base.py — `BrowserPaneViewModel`** (abstract): the pane contract.
  Subclasses override `TITLE` and `async _fetch`, and may call
  `_request_fetch` from their own sort/search mutators to trigger a refresh.
  `set_filter(topic_ids)` (sync, called by the orchestrator) is **idempotent**:
  no-op if the requested filter equals the pane's current `_filter_ids` AND
  `_filter_applied` is True (i.e. the pane has been set up at least once);
  otherwise cancels any in-flight fetch and spawns a new one. Cancellation
  is enforced by stamping each task with a `_current_task` identity, so a
  superseded task's `finally` quietly bows out instead of flipping
  `is_loading` back off. `topic_ids=None` is "no filter" — distinct from an
  empty iterable, which means "selection expanded to zero rows".

- **knowledge_entry_pane/ — `KnowledgeEntryBrowserPaneViewModel` +
  `KnowledgeEntryBrowserPaneView`** (split across `view_model.py` and
  `view.py`; see the subdir's own `CONTEXT.md` for the layout): the first
  concrete pane. VM window
  size is capped at `DEFAULT_PAGE_LIMIT` (500), with `load_more` appending
  the next page. `_fetch` runs the windowed SELECT first (so rows can
  paint before the total lands), then a separate COUNT for the "showing N
  of M" hint, with `_has_more` reconciled against the authoritative count.
  `set_search` / `set_sort` reset the window and cursor; `load_more`
  extends in place. `_cursor` is a window-local index (not an entry id),
  so it points at the same row before and after a `load_more`.

  The pane VM owns a child `EntryDetailsViewModel` (see below) exposed via
  the `details` property. `_sync_details()` pushes the cursor's entry
  (or `None`) into it; it's called from `set_cursor` and at the end of
  `_fetch`. Critically, `set_cursor` does **not** emit `dirty` itself —
  the pane view's `_refresh` rebuilds the `DataTable` in full, and
  rebuilding while the cursor is mid-move would feedback-loop with
  `DataTable.RowHighlighted`. Cursor moves are visible via the table's
  own cursor render and via the detail panel's separate dirty.

  The view is a `Vertical` containing a `Horizontal #pane-body` (table on
  the left at 60%, `EntryDetailsView` on the right at 40%) and a one-line
  `Static` status row docked to the bottom showing "loading…" / "showing
  N of M" / "N entries" / "no entries" based on VM state. Full table
  rebuild on every pane-VM `dirty`; after the rebuild the view calls
  `table.move_cursor(row=vm.cursor)` to restore the cursor position
  (`DataTable.clear()` resets it to row 0). `RowHighlighted` round-trips
  back through `vm.set_cursor`; the VM's equality early-return prevents
  loops. **No search bar yet** — deliberately benched for now; the seam
  for adding it is `set_search` on the VM and a `_search-bar` Static
  above the body.

- **knowledge_entry_pane/entry_details/ — `EntryDetailsViewModel` +
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

  **Choices list.** When `is_dirty` is true, a two-line `_ChoicesList`
  (Accept / Cancel) reveals below the content area. It's a focusable
  `Static` with its own bindings: up/down (`vm.move_choice_cursor`)
  and enter (dispatches `vm.accept()` or `vm.cancel()` by
  `choice_cursor`). When clean, it's hidden via a `.-visible` class
  toggle.

  **Accept path**: opens a session, calls `update_entry` + `commit`,
  then mutates the in-memory `KnowledgeEntry` instance in place so the
  pane VM's `self._entries[cursor]` reference picks up the new values
  without a refetch. Emits both `dirty` and a dedicated `SAVED`
  callback group; the pane VM subscribes to `SAVED` and emits its own
  `dirty` so the `DataTable` row repaints with the new title.

  **Cancel path**: restores the buffers to the entry's stored values
  and emits `dirty`. Choices list disappears on the next refresh.

  The VM has no subscriptions of its own. The pane VM is the only writer
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
  the window + count pair powering the knowledge-entries pane. Share an
  internal `_apply_entry_filters` helper so the count matches the window
  exactly (same topic-id and search semantics).

## Cross-region focus navigation (alt+left / alt+right)

This is a **view-side concern** — no VM knows or cares which sub-region
is focused. The dispatch tree:

```
BrowserView
 ├─ Topic tree
 └─ Active pane view
     ├─ table region
     └─ details region (EntryDetailsView)
         ├─ title TextArea
         ├─ content TextArea
         └─ choices list  (skipped when hidden)
```

`BrowserView` binds `alt+left` / `alt+right` with `priority=True` (so
they fire even when a `TextArea` inside the details panel is focused —
otherwise the TextArea's own word-nav bindings would swallow them).
Dispatch by `screen.focused` location:

- focus in tree, `alt+right` → call `active_pane_view.focus_first()`;
  `alt+left` → no-op.
- focus elsewhere, `alt+right` → call `pane.focus_next_region()`; if it
  returns False (pane is at its rightmost edge) do nothing.
- focus elsewhere, `alt+left` → call `pane.focus_prev_region()`; if it
  returns False (pane is at its leftmost edge), focus the tree.

**Pane-view interface** (convention; no formal Protocol yet — duck-typed
via `hasattr` until we have a second pane to share the contract with):

```python
def focus_first(self) -> None:           # leftmost sub-region (entry from tree)
def focus_next_region(self) -> bool:     # True if moved, False at rightmost edge
def focus_prev_region(self) -> bool:     # True if moved, False at leftmost edge
```

`KnowledgeEntryBrowserPaneView` cycles table → details and delegates the
details region's internal cycle to `EntryDetailsView`, which walks
`_REGION_IDS = (title, content, choices)` and skips entries whose
`widget.display` is False (i.e. the choices entry while clean).

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

Both `BrowserPaneViewModel.set_filter` and `list_entries_paginated` honor
this distinction. The orchestrator's `_current_filter` and the pane's
`_filter_ids` are both `frozenset[int] | None` to make the type echo the
semantics.
