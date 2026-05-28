"""TopicTreePanelView — the browser's left rail: action menu + topic tree.

Layout::

    ┌─ TopicTreePanelView ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, height: 1fr
    │ └─────────┴───────────────────┘ │
    └─────────────────────────────────┘

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

from ..topic_tree import BrowserTopicTreeView
from .action_menu import ActionMenuView
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

    @property
    def view_model(self) -> TopicTreePanelViewModel:
        return self._vm

    def compose(self):
        yield Static("Topics", id="browser-tree-title")
        with Horizontal(id="browser-tree-body"):
            yield ActionMenuView(
                on_rename=self._action_rename,
                on_create=self._action_create,
                on_delete=self._action_delete,
                id="browser-tree-actions",
            )
            yield BrowserTopicTreeView(self._vm.tree)

    # ------------------------------------------------------------------
    # Action stubs invoked by ActionMenuView
    # ------------------------------------------------------------------

    async def _action_rename(self) -> None:
        _logger.info("rename_topic stub — cursor=%s", self._vm.tree.cursor_topic_id)

    async def _action_create(self) -> None:
        _logger.info("create_topic stub — cursor parent=%s", self._vm.tree.cursor_topic_id)

    async def _action_delete(self) -> None:
        _logger.info(
            "delete_topic (subtree) stub — cursor=%s, selection=%d",
            self._vm.tree.cursor_topic_id,
            len(self._vm.tree.selected_ids),
        )

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
        if self._focus_is_in_tree():
            try:
                self.query_one(ActionMenuView).focus()
                return True
            except Exception:
                return False
        return False

    def nav_right(self) -> bool:
        if self._focus_is_in_actions():
            self.focus_tree()
            return True
        return False

    def nav_up(self) -> bool:
        # No "up" out of the tree or actions menu — the panel has no row above the body.
        return False

    def nav_down(self) -> bool:
        # No "down" — the panel has no row below the body.
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
            actions = self.query_one(ActionMenuView)
        except Exception:
            return False
        return focused is actions or actions in focused.ancestors_with_self
