# rhizome/tui/widgets/browser/topic_tree_panel/

The browser's left rail bundled as one panel: action menu + topic tree + topic summary, behind
a single VM + view so `BrowserView` / `BrowserViewModel` treat the rail as one region.

## Files

- **view_model.py — `TopicTreePanelViewModel`**: owns the three child VMs (tree, actions,
  summary) and the wires between them (cursor → summary; selection re-emitted as
  `filter_changed`).
- **view.py — `TopicTreePanelView`**: `Vertical(title, Horizontal(actions, tree), summary)`.
  Owns rail CSS and the cross-region focus surface (`focus_tree`, `nav_left`, `nav_right`)
  called by `BrowserView`.
- **topic_tree_actions.py — `TopicTreeActionsViewModel` + `TopicTreeActionsView`**: the
  vertical action menu to the left of the tree (rename / create / delete; action methods are
  stubs). `ChoiceList` subclass; collapsed-by-default with focus-driven rail expansion.

The tree itself (`BrowserTopicTreeViewModel` + `BrowserTopicTreeView`) and the summary
(`TopicSummaryViewModel` + `TopicSummaryView`) live at the parent level — this panel only
composes them.

## Conventions

- **Panel is the leftmost top-level region.** `nav_left` returning `False` is the leftmost-edge
  signal; `BrowserView` no-ops rather than trying to advance further left. There is no
  `nav_up` / `nav_down` — nothing focusable sits above or below the body row.
- **Rail expansion is panel-owned.** `TopicTreeActionsView.on_focus` toggles
  `-actions-expanded` on the surrounding `TopicTreePanelView` via
  `screen.query_one("TopicTreePanelView")` (type-name string to avoid the circular import the
  panel view induces by importing the actions widget). The right pane's `width: 1fr` absorbs
  the width difference automatically.
- **Filter contract is panel-shaped.** The orchestrator subscribes to `panel.filter_changed`
  and reads `panel.current_filter` — never reaches into `panel.tree`.
