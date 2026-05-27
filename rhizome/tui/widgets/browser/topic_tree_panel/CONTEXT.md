# rhizome/tui/widgets/browser/topic_tree_panel/

The browser's left rail bundled as one panel: action menu + topic tree + topic summary, behind
a single VM + view so `BrowserView` / `BrowserViewModel` treat the rail as one region.

## Files

- **view_model.py — `TopicTreePanelViewModel`**: owns the tree + summary child VMs and the
  wires between them (cursor → summary). Exposes `current_filter` as a composite read. The
  actions menu is VM-less, so the panel VM does not carry an actions child.
- **view.py — `TopicTreePanelView`**: `Vertical(title, Horizontal(actions, tree), summary)`.
  Owns rail CSS, the cross-region focus surface (`focus_tree`, `nav_left`, `nav_right`) called
  by `BrowserView`, and the rename / create / delete action stubs that `TopicTreeActionsView`
  invokes via callbacks supplied at compose time.
- **topic_tree_actions.py — `TopicTreeActionsView`**: vertical action menu to the left of the
  tree (rename / create / delete). VM-less `ChoiceList` subclass — action methods dispatch to
  callbacks injected by the panel view rather than to a VM. Collapsed-by-default with
  focus-driven rail expansion.

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
- **Filter contract.** The orchestrator subscribes to `panel.tree.selection_changed` for the
  event and reads `panel.current_filter` for the value. The panel intentionally does not
  re-broadcast the tree's signal under a panel-level alias.
