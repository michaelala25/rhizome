"""TopicTreePanelView — the browser's left rail: action menu + topic tree + summary/rename dialog.

Layout::

    ┌─ TopicTreePanelView ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, height: 1fr
    │ └─────────┴───────────────────┘ │
    │ <summary OR rename dialog>      │  ← #browser-tree-bottom (one at a time)
    └─────────────────────────────────┘

Bottom-slot mutex: the topic summary and the rename dialog are both mounted; the panel toggles a
``-active-rename`` class on itself to swap which is visible. The active dialog (today only the
rename one, more later) is captured in ``_active_dialog`` for the nav-surface routing below.

Rail expansion: the actions widget toggles ``-actions-expanded`` on this view when it gains/loses
focus, which CSS uses to widen the rail. ``BrowserView`` doesn't participate — the right pane's
``width: 1fr`` absorbs the difference.

Alt-arrow navigation: the panel owns its own ``alt+arrow`` bindings and resolves one step within
its focus graph via ``nav_<dir>``. When a step has no in-graph target, the action raises
``SkipAction`` so the key bubbles to ``BrowserView`` for cross-region handling (e.g., ``alt+right``
at the rightmost panel widget hops into the active tab).
"""

from __future__ import annotations

from typing import Any

from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from rhizome.logs import get_logger

from ..topic_summary import TopicSummaryView
from ..topic_tree import BrowserTopicTreeView
from .rename_dialog import RenameTopicDialogView
from .topic_tree_actions import TopicTreeActionsView
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
    TopicTreePanelView #browser-tree-summary,
    TopicTreePanelView #browser-tree-rename {
        height: auto;
        max-height: 50%;
        border-top: solid #3a3a3a;
        padding: 0;
    }
    /* Bottom-slot mutex: every dialog is ``display: none`` until ``.-visible`` is set on it;
       ``.-dialog-active`` on the panel hides the summary so the active dialog claims the slot. */
    TopicTreePanelView #browser-tree-rename { display: none; }
    TopicTreePanelView #browser-tree-rename.-visible { display: block; }
    TopicTreePanelView.-dialog-active #browser-tree-summary { display: none; }
    """

    BINDINGS = [
        Binding("alt+left", "nav_left", show=False),
        Binding("alt+right", "nav_right", show=False),
        Binding("alt+up", "nav_up", show=False),
        Binding("alt+down", "nav_down", show=False),
    ]

    def __init__(self, view_model: TopicTreePanelViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # ``None`` when the bottom slot shows the summary; a string id when a dialog is active.
        self._active_dialog: str | None = None

    @property
    def view_model(self) -> TopicTreePanelViewModel:
        return self._vm

    def compose(self):
        yield Static("Topics", id="browser-tree-title")
        with Horizontal(id="browser-tree-body"):
            yield TopicTreeActionsView(
                on_rename=self._action_rename,
                on_create=self._action_create,
                on_delete=self._action_delete,
                id="browser-tree-actions",
            )
            yield BrowserTopicTreeView(self._vm.tree)
        yield TopicSummaryView(self._vm.summary, id="browser-tree-summary")
        dialog = RenameTopicDialogView(self._vm.rename, on_done=self._rename_done)
        dialog.id = "browser-tree-rename"
        yield dialog

    # ------------------------------------------------------------------
    # Action stubs invoked by TopicTreeActionsView
    # ------------------------------------------------------------------

    async def _action_rename(self) -> None:
        # No-op if there's no cursor topic yet (empty tree / pre-mount).
        if self._vm.tree.cursor_topic_id is None:
            return
        self._vm.rename.sync_to(
            self._vm.tree.cursor_topic_id,
            self._vm.tree.cursor_topic_name,
        )
        dialog = self._rename_dialog()
        dialog.prepare_for_show()
        self._show_dialog("browser-tree-rename")
        dialog.focus_input()

    async def _action_create(self) -> None:
        _logger.info("create_topic stub — cursor parent=%s", self._vm.tree.cursor_topic_id)

    async def _action_delete(self) -> None:
        _logger.info(
            "delete_topic (subtree) stub — cursor=%s, selection=%d",
            self._vm.tree.cursor_topic_id,
            len(self._vm.tree.selected_ids),
        )

    # ------------------------------------------------------------------
    # Dialog mutex
    # ------------------------------------------------------------------

    def _show_dialog(self, widget_id: str) -> None:
        self._active_dialog = widget_id
        try:
            self.query_one(f"#{widget_id}").set_class(True, "-visible")
        except Exception:
            pass
        self.set_class(True, "-dialog-active")

    def _hide_dialog(self) -> None:
        if self._active_dialog is not None:
            try:
                self.query_one(f"#{self._active_dialog}").set_class(False, "-visible")
            except Exception:
                pass
        self._active_dialog = None
        self.set_class(False, "-dialog-active")

    def _rename_done(self, success: bool) -> None:
        # Called from the dialog after accept (commit attempted) or cancel. On success, repaint
        # the renamed node's label and the cached cursor name. Regardless, hide the dialog and
        # park focus back on the actions menu so the user can immediately invoke another action.
        if success:
            target_id = self._vm.rename.target_topic_id
            new_name = self._vm.rename.target_topic_name
            if target_id is not None and new_name is not None:
                try:
                    tree_view = self.query_one(BrowserTopicTreeView)
                    tree_view.update_node_label(target_id, new_name)
                except Exception:
                    pass
                self._vm.tree.update_cursor_topic_name(new_name)
        self._hide_dialog()
        try:
            self.query_one(TopicTreeActionsView).focus()
        except Exception:
            pass

    def _rename_dialog(self) -> RenameTopicDialogView:
        return self.query_one(RenameTopicDialogView)

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

    # Override so external ``panel.focus()`` calls land on the tree rather than no-op'ing on the
    # non-focusable ``Vertical`` container.
    def focus(self, scroll_visible: bool = True) -> "TopicTreePanelView":
        self.focus_tree()
        return self

    def focus_tree(self) -> None:
        try:
            self.query_one(BrowserTopicTreeView).focus()
        except Exception:
            pass

    def nav_left(self) -> bool:
        # In-dialog left = leftmost-edge no-op (consistent with the actions menu).
        if self._focus_is_in_dialog():
            return False
        if self._focus_is_in_tree():
            try:
                self.query_one(TopicTreeActionsView).focus()
                return True
            except Exception:
                return False
        return False

    def nav_right(self) -> bool:
        # In-dialog right → caller (BrowserView) advances into the active tab.
        if self._focus_is_in_dialog():
            return False
        if self._focus_is_in_actions():
            self.focus_tree()
            return True
        return False

    def nav_up(self) -> bool:
        if self._focus_is_in_dialog():
            result = self._rename_dialog().nav_up()
            if result == "tree":
                self.focus_tree()
                return True
            return bool(result)
        # No "up" out of the tree or actions menu — the panel has no row above the body.
        return False

    def nav_down(self) -> bool:
        if self._focus_is_in_dialog():
            return self._rename_dialog().nav_down()
        # Tree → dialog input (when dialog is live).
        if self._focus_is_in_tree() and self._active_dialog == "browser-tree-rename":
            self._rename_dialog().focus_input()
            return True
        return False

    # ------------------------------------------------------------------
    # Focus probes
    # ------------------------------------------------------------------

    def _focus_is_in_tree(self) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        try:
            tree = self.query_one(BrowserTopicTreeView)
        except Exception:
            return False
        return focused is tree or tree in focused.ancestors_with_self

    def _focus_is_in_actions(self) -> bool:
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return False
        try:
            actions = self.query_one(TopicTreeActionsView)
        except Exception:
            return False
        return focused is actions or actions in focused.ancestors_with_self

    def _focus_is_in_dialog(self) -> bool:
        if self._active_dialog is None:
            return False
        try:
            return self._rename_dialog().focus_is_inside()
        except Exception:
            return False
