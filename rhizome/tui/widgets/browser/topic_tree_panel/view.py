"""TopicTreePanelView — the browser's left rail: action menu + topic tree + summary.

Owns the rail CSS and the cross-region focus contract that ``BrowserView`` dispatches against.
Layout::

    ┌─ TopicTreePanelView ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, ``height: 1fr``
    │ └─────────┴───────────────────┘ │
    │ ──────────────────────────────  │  ← #browser-tree-summary border-top
    │ Topic summary                   │  ← #browser-tree-summary (auto height, capped)
    └─────────────────────────────────┘

Rail expansion: when the actions menu is focused, ``TopicTreeActionsView.on_focus`` toggles the
``-actions-expanded`` class on *this* widget (via a ``screen.query_one("TopicTreePanelView")``
lookup; type-name string to avoid a circular import). The CSS then widens the rail and switches
the rule colour to its focus-bright variant. ``BrowserView`` doesn't participate — the right pane
uses ``width: 1fr`` so it absorbs the difference automatically.

Cross-region focus contract — ``BrowserView`` calls these on the panel without knowing what's
inside:

  * ``focus_tree()`` — focus the topic tree specifically. Called when the active tab's
    ``nav_left`` returns the ``"topic_tree"`` sentinel (i.e. focus arriving back from the right
    pane should land on the tree, not the actions menu).
  * ``nav_left() -> bool`` — internal left move within the panel. Returns ``True`` if it moved
    focus (tree → actions); ``False`` if focus is already at the leftmost sub-region (actions),
    signalling ``BrowserView`` to no-op (the panel is the leftmost top-level region).
  * ``nav_right() -> bool`` — internal right move within the panel. Returns ``True`` (actions →
    tree) or ``False`` if focus is already at the rightmost sub-region (the tree), signalling
    ``BrowserView`` to advance into the next top-level region (the active tab's leftmost cell).

The panel doesn't expose ``nav_up`` / ``nav_down`` — there's no focusable sub-region stacked above
or below; ``BrowserView`` no-ops alt+up/alt+down while focus is in the panel.
"""

from __future__ import annotations

from typing import Any

from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ..topic_summary import TopicSummaryView
from ..topic_tree import BrowserTopicTreeView
from .topic_tree_actions import TopicTreeActionsView
from .view_model import TopicTreePanelViewModel


class TopicTreePanelView(Vertical):
    """View for ``TopicTreePanelViewModel``. See module docstring for the layout, expansion
    behaviour, and cross-region focus contract."""

    DEFAULT_CSS = """
    TopicTreePanelView {
        width: 23%;
        border: solid #3a3a3a;
        padding: 0;
    }
    /* While the actions menu is focused, widen the rail by ~1.66x so the full action labels
       (rendered in place of the single-letter shorthand) fit. The right pane uses ``width: 1fr``
       so it absorbs the difference automatically. */
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
    /* Vertical rule between the actions menu and the tree. Lives on the tree (not the menu) so it
       spans the full body height regardless of how few action rows the menu currently renders. */
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
        # Body row: actions menu on the left, topic tree on the right. The menu is narrow when
        # blurred (just single-letter shorthand) and widens — along with the entire panel — when
        # focused. The vertical rule between the two lives on the tree's ``border-left`` so it
        # spans the full body height.
        with Horizontal(id="browser-tree-body"):
            yield TopicTreeActionsView(self._vm.tree_actions, id="browser-tree-actions")
            yield BrowserTopicTreeView(self._vm.tree)
        # Topic summary panel for the cursor-highlighted topic. Sits below the body, growing to fit
        # its (multi-line description) content; ``max-height: 50%`` keeps a long description from
        # squeezing the tree out of view.
        yield TopicSummaryView(self._vm.summary, id="browser-tree-summary")

    # ------------------------------------------------------------------
    # Cross-region focus navigation (called from BrowserView)
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
        # Focus is already at the actions menu (or somewhere unexpected) — leftmost edge.
        return False

    def nav_right(self) -> bool:
        if self._focus_is_in_actions():
            self.focus_tree()
            return True
        # Focus in the tree — rightmost sub-region of the panel.
        return False

    # ------------------------------------------------------------------
    # Internal focus introspection
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
