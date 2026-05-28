# rhizome/tui/widgets/browser/topic_tree_panel/

The browser's left rail bundled as one panel: action menu + topic tree + topic-details, behind a
single VM + view so `BrowserView` / `BrowserViewModel` treat the rail as one region.

## Files

- **view_model.py — `TopicTreePanelViewModel`**: owns the tree + details child VMs and the wire
  between them (`tree.cursor_changed` → `details.set_topic_id`). Exposes `current_filter` as a
  composite read. The actions menu is VM-less.
- **view.py — `TopicTreePanelView`**: `Vertical(title, Horizontal(actions, tree), details)`.
  Owns rail CSS, the cross-region focus surface (`nav_left/right/up/down`) called by
  `BrowserView`, and subscribes to `details.saved` to repaint the tree node label after a rename.
- **action_menu.py — `ActionMenuView`**: vertical action menu to the left of the tree (rename /
  create / delete). VM-less `ChoiceList` subclass — posts nested `RenameRequested` /
  `CreateRequested` / `DeleteRequested` messages caught by the panel view's
  `on_action_menu_view_<name>_requested` handlers. Collapsed-by-default with focus-driven rail
  expansion.
- **topic_details/ — `TopicDetailsViewModel` + `TopicDetailsView`**: buffered-edit panel beneath
  the tree (name + description `TextArea`s + Accept/Cancel choices). VM is a
  `QueryBackedViewModel`; the panel VM pushes `set_topic_id` on cursor changes, the fetch loads
  the topic, and reseeding buffers silently discards any in-progress edit. Accept persists via
  `update_topic` and emits `SAVED` so the panel view can repaint the tree node label.

The tree itself (`BrowserTopicTreeViewModel` + `BrowserTopicTreeView`) lives at the parent level
— this panel only composes it.

## Conventions

- **Panel is the leftmost top-level region.** `nav_left` returning `False` is the leftmost-edge
  signal; `BrowserView` no-ops rather than trying to advance further left.
- **Vertical nav extends through the details panel.** From the tree, `alt+down` walks
  `tree → details_name → details_description → details_accept` (the last only when the details
  panel `is_dirty`); `alt+up` is the reverse. The actions menu has no up/down neighbours.
- **Pane-wide shortcuts.** `r` focuses the details name field. `c` creates under the cursor
  parent (with `cursor_topic_id is None` — i.e., cursor on the tree's synthetic `(root)` row —
  meaning "create at root level"); `shift+c` always creates at root. All three are inert inside
  the details `TextArea`s since those consume printable keys at the widget level.
- **Rail expansion is panel-owned.** `ActionMenuView.on_focus` toggles `-actions-expanded` on the
  surrounding `TopicTreePanelView` via `screen.query_one("TopicTreePanelView")` (type-name string
  to avoid the circular import the panel view induces by importing the actions widget). The right
  pane's `width: 1fr` absorbs the width difference automatically.
- **Filter contract.** The orchestrator subscribes to `panel.tree.selection_changed` for the
  event and reads `panel.current_filter` for the value. The panel intentionally does not
  re-broadcast the tree's signal under a panel-level alias.
