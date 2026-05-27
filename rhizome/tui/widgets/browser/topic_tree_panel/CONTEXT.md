# rhizome/tui/widgets/browser/topic_tree_panel/

The browser's left rail: action menu + topic tree + topic summary, bundled
behind a single VM + view so the top-level `BrowserView` / `BrowserViewModel`
can treat the whole rail as one region.

## Files

- **topic_tree_actions.py — `TopicTreeActionsViewModel` +
  `TopicTreeActionsView`**: vertical action menu sitting to the left of
  the topic tree inside the panel. Lives here because nothing outside
  the panel composes or imports it. Subclasses `ChoiceList` (vertical
  orientation) so it gets the cursor + enter/escape + dispatch wiring
  for free. Choices for now: `rename`, `create`, `delete` (subtree) —
  each dispatched via `ChoiceList`'s action-method protocol to a `do_*`
  handler that awaits a VM stub. The VM holds a reference to the
  `BrowserTopicTreeViewModel` so handlers can read cursor / selection
  state directly.

  Collapsed by default: when unfocused the widget renders just the
  single-letter shorthand (`R` / `C` / `D`) with no cursor marker and
  the panel sits at its default ~23% width. On focus, `on_focus`
  toggles a `-actions-expanded` class on the surrounding
  `TopicTreePanelView` (via a `screen.query_one("TopicTreePanelView")`
  string-type query to avoid a circular import) so the rail widens to
  ~33%, and `_render_choice` switches to the full `► label` rendering.
  `on_blur` reverses both.

  Action methods are stubs for now (they log cursor / selection state).
  Concrete handlers — dialogs, DB calls — land in follow-up passes.

- **view_model.py — `TopicTreePanelViewModel`**: owns the three child VMs
  (`BrowserTopicTreeViewModel`, `TopicTreeActionsViewModel`,
  `TopicSummaryViewModel`) and the wires between them:
    * subscribes to `tree.cursor_changed` → `summary.set_topic_id` so the
      cursor drives the summary fetch;
    * subscribes to `tree.selection_changed` → re-emits its own
      `FILTER_CHANGED` callback so the orchestrator listens once at the
      panel boundary instead of reaching through `panel.tree`.
  Exposes read-only accessors for each child VM (so the view can wire each
  sub-view at compose time), the `filter_changed` group, and a sync
  `current_filter` property that delegates to
  `tree.expanded_filter_ids()`. No async `start()` — child views load their
  own state on mount.

- **view.py — `TopicTreePanelView`**: `Vertical(title, Horizontal(actions,
  tree), summary)`. Owns the rail CSS (width, border, expansion on
  `-actions-expanded`, vertical-rule via `BrowserTopicTreeView`'s
  `border-left`, summary border-top) and the cross-region focus contract
  used by `BrowserView`:
    * `focus_tree()` — focus the topic tree (target of the tab's
      `"topic_tree"` sentinel when alt+left arrives back from the right
      pane).
    * `nav_left() -> bool` — internal left move (tree → actions). Returns
      `False` when focus is already in the actions menu (panel is the
      leftmost top-level region; `BrowserView` no-ops).
    * `nav_right() -> bool` — internal right move (actions → tree). Returns
      `False` when focus is in the tree (`BrowserView` advances focus into
      the active tab's `focus_first`).
  No `nav_up` / `nav_down` — there's no focusable sub-region above or below
  the body row; `BrowserView` no-ops alt+up/alt+down while focus is in the
  panel.

## Conventions

- **Panel is leftmost.** `nav_left` returning `False` is the "leftmost
  edge" signal; `BrowserView` interprets it as a no-op rather than trying
  to advance out of the panel.
- **Rail expansion is panel-owned.** `TopicTreeActionsView.on_focus` walks
  up via `screen.query_one("TopicTreePanelView")` (type-name string to
  avoid a circular import) and toggles `-actions-expanded` on the panel.
  The CSS then widens the rail; the right pane uses `width: 1fr` so it
  absorbs the difference automatically — `BrowserView` doesn't participate.
- **Filter contract is panel-shaped.** The orchestrator subscribes to
  `panel.filter_changed` and reads `panel.current_filter`. It never
  reaches through into `panel.tree`.
