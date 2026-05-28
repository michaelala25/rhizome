"""TopicTreePanelView — the browser's left rail: action menu + topic tree + topic-details panel.

Layout::

    ┌─ TopicTreePanelView ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, height: 1fr
    │ └─────────┴───────────────────┘ │
    │  topic-details (name/desc)      │  ← #browser-topic-details
    └─────────────────────────────────┘

Rail expansion: the actions widget toggles ``-actions-expanded`` on this view when it gains/loses
focus, which CSS uses to widen the rail. ``BrowserView`` doesn't participate — the right pane's
``width: 1fr`` absorbs the difference.

Alt-arrow navigation: the panel owns its own ``alt+arrow`` bindings and resolves one step within
its focus graph via ``nav_<dir>``. When a step has no in-graph target, the action raises
``SkipAction`` so the key bubbles to ``BrowserView`` for cross-region handling (e.g., ``alt+right``
at the rightmost panel widget hops into the active tab).

Focus graph (alt-arrows):

  * alt+down:  tree → details_name → details_description → details_accept (when dirty)
  * alt+up:    reverse — details_accept → details_description → details_name → tree
  * alt+left:  tree → actions; details fields → no-op (leftmost)
  * alt+right: actions → tree; details fields → no-op (caller hops to active tab)
"""

from __future__ import annotations

from typing import Any

from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from rhizome.logs import get_logger

from ..topic_tree import BrowserTopicTreeView
from .action_menu import ActionMenuView
from .delete_dialog import DeleteDialogView
from .topic_details import TopicDetailsView
from .view_model import TopicTreePanelViewModel

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanelView(Vertical):
    """View for ``TopicTreePanelViewModel``. See module docstring."""

    DEFAULT_CSS = """
    TopicTreePanelView {
        width: 23%;
        border: solid #3a3a3a;
        padding: 0;
    }
    /* Widen when the actions menu is focused so the full labels (rendered in place of the
       single-letter shorthand) fit. */
    TopicTreePanelView.-actions-expanded {
        width: 33%;
    }
    TopicTreePanelView:focus-within {
        border: solid #6a6a6a;
    }
    TopicTreePanelView #browser-tree-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    TopicTreePanelView #browser-tree-body {
        height: 1fr;
    }
    /* Vertical rule between the actions menu and the tree lives on the tree (not the menu) so
       it spans the full body height regardless of how few action rows the menu renders. */
    TopicTreePanelView BrowserTopicTreeView {
        padding: 1 0 0 1;
        height: 1fr;
        border-left: solid #3a3a3a;
    }
    TopicTreePanelView.-actions-expanded BrowserTopicTreeView {
        border-left: solid #6a6a6a;
    }
    /* Bottom-slot mutex: ``-deleting`` on this panel swaps the details widget out for the
       confirm-delete dialog. */
    TopicTreePanelView.-deleting TopicDetailsView { display: none; }
    TopicTreePanelView.-deleting DeleteDialogView { display: block; }
    """

    BINDINGS = [
        Binding("alt+left", "nav_left", show=False),
        Binding("alt+right", "nav_right", show=False),
        Binding("alt+up", "nav_up", show=False),
        Binding("alt+down", "nav_down", show=False),
        Binding("r", "rename", show=False),
        # ``c`` creates under the cursor (``(root)`` cursor → no parent); ``shift+c`` always at root.
        Binding("c", "create", show=False),
        Binding("shift+c", "create_root", show=False),
        Binding("d", "delete", show=False),
    ]

    def __init__(self, view_model: TopicTreePanelViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Topic id captured at delete-dispatch time. Pins the destructive op to the cursor topic
        # *as of* when the dialog opened, so an alt+up→tree→arrow→alt+down round-trip can't end
        # up deleting a different topic than the one the prompt named.
        self._delete_target_id: int | None = None

    @property
    def view_model(self) -> TopicTreePanelViewModel:
        return self._vm

    def compose(self):
        yield Static("Topics", id="browser-tree-title")
        with Horizontal(id="browser-tree-body"):
            yield ActionMenuView(id="browser-tree-actions")
            yield BrowserTopicTreeView(self._vm.tree)
        yield TopicDetailsView(self._vm.details, id="browser-topic-details")
        yield DeleteDialogView(id="browser-topic-delete")

    def on_mount(self) -> None:
        # Repaint the tree node's label after a successful Accept on the details panel.
        self._vm.details.subscribe(self._vm.details.saved, self._on_details_saved)

    def on_unmount(self) -> None:
        self._vm.details.unsubscribe(self._vm.details.saved, self._on_details_saved)

    def _on_details_saved(self) -> None:
        topic = self._vm.details.topic
        if topic is None:
            return
        try:
            self.query_one(BrowserTopicTreeView).update_node_label(topic.id, topic.name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ActionMenuView message handlers — stubs for now; real dialogs land later.
    # ------------------------------------------------------------------

    def on_action_menu_view_rename_requested(
        self, event: ActionMenuView.RenameRequested
    ) -> None:
        # "Rename" is just a shortcut to the name field in the details panel — the buffered-edit
        # flow there already handles the commit. No-ops if no topic is loaded.
        self._focus_details_node("name")

    def on_action_menu_view_create_requested(
        self, event: ActionMenuView.CreateRequested
    ) -> None:
        self._dispatch_create(self._vm.tree.cursor_topic_id)

    def on_action_menu_view_delete_requested(
        self, event: ActionMenuView.DeleteRequested
    ) -> None:
        self._begin_delete()

    def _begin_delete(self) -> None:
        # No-op when cursor is on the synthetic ``(root)`` row — there's no topic to delete.
        topic_id = self._vm.tree.cursor_topic_id
        if topic_id is None:
            return
        topic_name: str | None = None
        if self._vm.details.topic is not None and self._vm.details.topic.id == topic_id:
            topic_name = self._vm.details.topic.name
        self._delete_target_id = topic_id
        dialog = self.query_one(DeleteDialogView)
        dialog.prepare_for_show(topic_name)
        self.add_class("-deleting")
        dialog.focus()

    def on_delete_dialog_view_accepted(
        self, event: DeleteDialogView.Accepted
    ) -> None:
        self.run_worker(self._delete_worker(), exclusive=False)

    def on_delete_dialog_view_cancelled(
        self, event: DeleteDialogView.Cancelled
    ) -> None:
        self._end_delete()

    async def _delete_worker(self) -> None:
        # Read the pinned target — NOT the live cursor (see __init__ note).
        topic_id = self._delete_target_id
        if topic_id is None:
            self._end_delete()
            return
        await self._vm.tree.delete_topic_subtree(topic_id)
        try:
            self.query_one(BrowserTopicTreeView).remove_node(topic_id)
        except Exception:
            pass
        self._end_delete()

    def _end_delete(self) -> None:
        self._delete_target_id = None
        self.remove_class("-deleting")
        self.focus_tree()

    # ------------------------------------------------------------------
    # Alt-arrow nav — actions wrap the in-graph resolvers and bubble on no-handle
    # ------------------------------------------------------------------

    def action_nav_left(self) -> None:
        if not self.nav_left():
            raise SkipAction()

    def action_nav_right(self) -> None:
        if not self.nav_right():
            raise SkipAction()

    def action_nav_up(self) -> None:
        if not self.nav_up():
            raise SkipAction()

    def action_nav_down(self) -> None:
        if not self.nav_down():
            raise SkipAction()

    def action_rename(self) -> None:
        self._focus_details_node("name")

    def action_create(self) -> None:
        # Cursor on ``(root)`` → ``cursor_topic_id is None`` → create at root level.
        self._dispatch_create(self._vm.tree.cursor_topic_id)

    def action_create_root(self) -> None:
        self._dispatch_create(None)

    def action_delete(self) -> None:
        self._begin_delete()

    def _dispatch_create(self, parent_id: int | None) -> None:
        # Background worker because the DB write + tree mutation chain is async, and key-binding
        # action handlers must stay sync (Textual awaits them but we don't want to block the key
        # pipeline on the round-trip). The worker drives both halves so the cursor-move lands
        # only after the new node is actually mounted.
        self.run_worker(self._create_topic_worker(parent_id), exclusive=False)

    async def _create_topic_worker(self, parent_id: int | None) -> None:
        topic = await self._vm.tree.create_topic(parent_id)
        try:
            tree_view = self.query_one(BrowserTopicTreeView)
        except Exception:
            return
        await tree_view.add_created_topic(topic)

    # Override so external ``panel.focus()`` calls land on the tree rather than no-op'ing on the
    # non-focusable ``Vertical`` container.
    def focus(self, scroll_visible: bool = True) -> "TopicTreePanelView":
        self.focus_tree()
        return self

    def focus_tree(self) -> None:
        self._focus_widget("BrowserTopicTreeView")

    def nav_left(self) -> bool:
        node = self._focused_node()
        if node == "tree":
            return self._focus_widget("ActionMenuView")
        # Actions / details fields → leftmost edge (no-op; caller stays in panel).
        return False

    def nav_right(self) -> bool:
        node = self._focused_node()
        if node == "actions":
            self.focus_tree()
            return True
        # tree / details fields → False → caller (BrowserView) advances into the active tab.
        return False

    def nav_down(self) -> bool:
        node = self._focused_node()
        if node == "tree":
            # The delete dialog takes over the bottom slot while ``-deleting`` is active.
            if self._delete_active():
                return self._focus_delete_dialog()
            return self._focus_details_node("name")
        if node == "details_name":
            return self._focus_details_node("description")
        if node == "details_description":
            return self._focus_details_node("accept")
        # actions / details_accept / delete_dialog → no further down step.
        return False

    def nav_up(self) -> bool:
        node = self._focused_node()
        if node == "delete_dialog":
            self.focus_tree()
            return True
        if node == "details_accept":
            return self._focus_details_node("description")
        if node == "details_description":
            return self._focus_details_node("name")
        if node == "details_name":
            self.focus_tree()
            return True
        # tree / actions → no row above the body / details.
        return False

    # ------------------------------------------------------------------
    # Focus probes + dispatch
    # ------------------------------------------------------------------

    _DETAILS_NODE_IDS: dict[str, str] = {
        "name": "topic-details-name",
        "description": "topic-details-description",
        "accept": "topic-details-choices",
    }

    def _focused_node(self) -> str | None:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return None
        fid = focused.id
        if fid == "browser-topic-delete":
            return "delete_dialog"
        if fid == "topic-details-name":
            return "details_name"
        if fid == "topic-details-description":
            return "details_description"
        if fid == "topic-details-choices":
            return "details_accept"
        if self._focus_is_in_widget("BrowserTopicTreeView"):
            return "tree"
        if self._focus_is_in_widget("ActionMenuView"):
            return "actions"
        return None

    def _delete_active(self) -> bool:
        return self.has_class("-deleting")

    def _focus_delete_dialog(self) -> bool:
        try:
            self.query_one(DeleteDialogView).focus()
            return True
        except Exception:
            return False

    def _focus_details_node(self, key: str) -> bool:
        # ``accept`` is conditional on ``is_dirty`` — present in the graph only while the choices
        # row is rendered (``.-visible`` toggled in TopicDetailsView._refresh).
        if key == "accept" and not self._vm.details.is_dirty:
            return False
        if key in ("name", "description") and self._vm.details.topic is None:
            return False
        widget_id = self._DETAILS_NODE_IDS[key]
        try:
            self.query_one(f"#{widget_id}").focus()
            return True
        except Exception:
            return False

    def _focus_widget(self, type_name: str) -> bool:
        try:
            self.query_one(type_name).focus()
            return True
        except Exception:
            return False

    def _focus_is_in_widget(self, type_name: str) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        try:
            target = self.query_one(type_name)
        except Exception:
            return False
        return focused is target or target in focused.ancestors_with_self
