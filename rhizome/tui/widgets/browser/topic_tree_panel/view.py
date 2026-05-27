"""TopicTreePanelView — the browser's left rail: action menu + topic tree + summary.

Layout::

    ┌─ TopicTreePanelView ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, height: 1fr
    │ └─────────┴───────────────────┘ │
    │ Topic summary                   │  ← #browser-tree-summary (auto height, capped at 50%)
    └─────────────────────────────────┘

Rail expansion: the actions widget toggles ``-actions-expanded`` on this view when it gains/loses
focus, which CSS uses to widen the rail. ``BrowserView`` doesn't participate — the right pane's
``width: 1fr`` absorbs the difference.

Cross-region focus surface called by ``BrowserView``:

  * ``focus_tree()`` — focus the topic tree (target of the tab's ``"topic_tree"`` sentinel from
    ``alt+left``).
  * ``nav_left() -> bool`` — tree → actions; ``False`` if already in actions (leftmost edge, panel
    is the leftmost top-level region — ``BrowserView`` no-ops).
  * ``nav_right() -> bool`` — actions → tree; ``False`` if already in the tree (``BrowserView``
    advances into the active tab via ``tab.focus_first()``).

No ``nav_up`` / ``nav_down`` — nothing focusable sits above or below the body row.
"""

from __future__ import annotations

from typing import Any

from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from rhizome.logs import get_logger

from ..topic_summary import TopicSummaryView
from ..topic_tree import BrowserTopicTreeView
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
    TopicTreePanelView #browser-tree-summary {
        height: auto;
        max-height: 50%;
        border-top: solid #3a3a3a;
        padding: 0;
    }
    """

    def __init__(self, view_model: TopicTreePanelViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

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

    # ------------------------------------------------------------------
    # Action stubs invoked by TopicTreeActionsView
    # ------------------------------------------------------------------
    # These hook the actions menu into the panel without routing through a VM. Concrete dialogs
    # and DB calls land in follow-up passes; for now they log the tree's cursor / selection so the
    # dispatch wiring can be exercised end-to-end.

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
    # Cross-region focus surface (called from BrowserView)
    # ------------------------------------------------------------------

    def focus_tree(self) -> None:
        try:
            self.query_one(BrowserTopicTreeView).focus()
        except Exception:
            pass

    def nav_left(self) -> bool:
        if self._focus_is_in_tree():
            try:
                self.query_one(TopicTreeActionsView).focus()
                return True
            except Exception:
                return False
        return False

    def nav_right(self) -> bool:
        if self._focus_is_in_actions():
            self.focus_tree()
            return True
        return False

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
