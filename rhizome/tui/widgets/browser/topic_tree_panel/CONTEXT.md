# rhizome/tui/widgets/browser/topic_tree_panel/

The browser's left rail bundled as one panel: action menu + topic tree, behind a single VM + view
so `BrowserView` / `BrowserViewModel` treat the rail as one region.

## Files

- **view_model.py — `TopicTreePanelViewModel`**: owns the tree child VM and exposes
  `current_filter` as a composite read. The actions menu is VM-less, so the panel VM does not
  carry an actions child.
- **view.py — `TopicTreePanelView`**: `Vertical(title, Horizontal(actions, tree))`. Owns rail
  CSS, the cross-region focus surface (`focus_tree`, `nav_left/right/up/down`) called by
  `BrowserView`, and the rename / create / delete action stubs that `ActionMenuView` invokes via
  callbacks supplied at compose time.
- **action_menu.py — `ActionMenuView`**: vertical action menu to the left of the tree (rename /
  create / delete). VM-less `ChoiceList` subclass — action methods dispatch to callbacks injected
  by the panel view rather than to a VM. Collapsed-by-default with focus-driven rail expansion.

The tree itself (`BrowserTopicTreeViewModel` + `BrowserTopicTreeView`) lives at the parent level
— this panel only composes it.

## Conventions

- **Panel is the leftmost top-level region.** `nav_left` returning `False` is the leftmost-edge
  signal; `BrowserView` no-ops rather than trying to advance further left.
- **Vertical nav is a no-op.** Nothing focusable sits above or below the body row, so `nav_up`
  and `nav_down` both return `False`.
- **Rail expansion is panel-owned.** `ActionMenuView.on_focus` toggles `-actions-expanded` on the
  surrounding `TopicTreePanelView` via `screen.query_one("TopicTreePanelView")` (type-name string
  to avoid the circular import the panel view induces by importing the actions widget). The right
  pane's `width: 1fr` absorbs the width difference automatically.
- **Filter contract.** The orchestrator subscribes to `panel.tree.selection_changed` for the
  event and reads `panel.current_filter` for the value. The panel intentionally does not
  re-broadcast the tree's signal under a panel-level alias.
