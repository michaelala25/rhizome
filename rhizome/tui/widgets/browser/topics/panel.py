"""TopicTreePanel — the browser's left rail: action menu + topic tree + topic-details panel.

Layout::

    ┌─ TopicTreePanel ────────────┐
    │ Topics                          │  ← #browser-tree-title
    │ ┌─ #browser-tree-body ────────┐ │
    │ │ actions │  topic tree       │ │  ← horizontal row, height: 1fr
    │ └─────────┴───────────────────┘ │
    │  topic-details (name/desc)      │  ← #browser-topic-details
    └─────────────────────────────────┘

Width: panel takes a proportional slice of the ``Browser``'s horizontal space; the action menu
sizes to its content (``Actions`` header + longest label) and the topic tree fills whatever's left,
with a horizontal scrollbar when nodes overflow. ``-actions-expanded`` is set at construction; the
focus-driven auto-toggle that used to widen the rail is retired — the class remains as a skeleton
hook (see ``ActionMenu._set_pane_expanded``) but no automatic state-flipping happens on navigation.

Alt-arrow navigation: the panel uses ``FocusOrchestrationMixin`` to walk its focus graph (see
``FOCUS_GRAPH``). When a step has no in-graph target, ``action_focus_neighbour`` raises
``SkipAction`` so the key bubbles to ``Browser`` for cross-region handling (e.g., ``alt+right``
at the rightmost panel widget hops into the active tab).

Focus graph (alt-arrows):

  * alt+down:  topic-tree → (delete-dialog if active, else topic-details-name) →
               topic-details-description → topic-details-choices (when dirty)
  * alt+up:    reverse path
  * alt+left:  topic-tree → browser-tree-actions; details fields → bubble (caller stays in panel)
  * alt+right: browser-tree-actions → topic-tree; details fields → bubble (caller hops into tab)
"""

from __future__ import annotations

from typing import Any

from textual.actions import SkipAction
from textual.containers import Horizontal, Vertical
from textual.events import DescendantBlur, DescendantFocus
from textual.widgets import Static

from rhizome.logs import get_logger

from rhizome.tui.widgets.browser.topics.tree import TopicTree
from rhizome.tui.widgets.browser.topics.actions import ActionMenu
from rhizome.tui.widgets.browser.topics.delete import TopicsDeleteMenu
from rhizome.tui.widgets.browser.topics.details import TopicDetails
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind
from rhizome.app.browser.topics.panel import TopicTreePanelVM

_logger = get_logger("browser.topic_tree_panel")


class TopicTreePanel(Vertical, FocusOrchestrationMixin):
    """View for ``TopicTreePanelVM``. See module docstring."""

    # Vertical's own ``can_focus = False`` (in its ``__dict__``) wins MRO over the mixin's True,
    # so we restore it explicitly here — required for the mixin's ``on_focus`` delegation to fire
    # when external callers focus the panel.
    can_focus = True

    DEFAULT_CSS = """
    /* Panel takes a proportional slice of ``Browser`` (sibling to ``#browser-right-tab``'s
       ``width: 1fr``); inside, the action menu sizes to content and the tree fills the rest. */
    TopicTreePanel {
        width: 25%;
        border: solid #3a3a3a;
        padding: 0;
    }
    /* ``-focus-within`` is set manually from ``on_descendant_focus``/``on_descendant_blur`` rather
       than via the ``:focus-within`` pseudo-selector, which forces a full ancestor invalidation on
       every focus shift in the subtree. The class flip is local to this node. */
    TopicTreePanel.-focus-within {
        border: solid #6a6a6a;
    }
    TopicTreePanel #browser-tree-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    TopicTreePanel #browser-tree-body {
        height: 1fr;
    }
    /* Vertical rule between the actions menu and the tree lives on the tree (not the menu) so it
       spans the full body height regardless of how few action rows the menu renders. The tree
       fills the remaining body width and scrolls horizontally when deep / long node labels
       overflow. */
    TopicTreePanel TopicTree {
        width: 1fr;
        padding: 1 0 0 1;
        height: 1fr;
        border-left: solid #3a3a3a;
        overflow-x: auto;
    }
    TopicTreePanel.-actions-expanded TopicTree {
        border-left: solid #6a6a6a;
    }
    /* Bottom-slot mutex: ``-deleting`` on this panel swaps the details widget out for the
       confirm-delete dialog. */
    TopicTreePanel.-deleting TopicDetails { display: none; }
    TopicTreePanel.-deleting TopicsDeleteMenu { display: block; }
    """

    BINDINGS = [
        Keybind.FocusLeft .as_binding("focus_neighbour('left')",  show=False),
        Keybind.FocusRight.as_binding("focus_neighbour('right')", show=False),
        Keybind.FocusUp   .as_binding("focus_neighbour('up')",    show=False),
        Keybind.FocusDown .as_binding("focus_neighbour('down')",  show=False),
        Keybind.BrowserRenameTopic.as_binding("rename", "Rename", show=True),
        # ``c`` creates under the cursor (``(root)`` cursor → no parent); ``shift+c`` always at root.
        Keybind.BrowserCreateTopic.      as_binding("create",      "Create",         show=True),
        Keybind.BrowserCreateTopicAtRoot.as_binding("create_root", "Create at root", show=True),
        Keybind.BrowserDeleteTopic.      as_binding("delete",      "Delete",         show=True),
    ]

    # Static focus graph. Tree-down has a two-element fallback so when ``-deleting`` is active the
    # delete dialog wins (gated by ``has_class("-deleting")`` in ``_is_node_available``), otherwise
    # focus lands on the topic-name field — which is itself gated on ``details.topic is not None``,
    # so an empty cursor (``(root)`` row) just skips the details branch entirely.
    FOCUS_GRAPH = FocusGraph(
        source="topic-tree",
        edges={
            "topic-tree": {
                "left": "browser-tree-actions",
                "down": ["browser-topic-delete", "topic-details-name"],
            },
            "browser-tree-actions": {
                "right": "topic-tree",
            },
            "topic-details-name": {
                "up":   "topic-tree",
                "down": "topic-details-description",
            },
            "topic-details-description": {
                "up":   "topic-details-name",
                "down": "topic-details-choices",
            },
            "topic-details-choices": {
                "up": "topic-details-description",
            },
            "browser-topic-delete": {
                "up": "topic-tree",
            },
        },
    )

    def __init__(self, view_model: TopicTreePanelVM, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Topic id captured at delete-dispatch time. Pins the destructive op to the cursor topic
        # *as of* when the dialog opened, so an alt+up→tree→arrow→alt+down round-trip can't end
        # up deleting a different topic than the one the prompt named.
        self._delete_target_id: int | None = None
        # Actions menu is permanently expanded — the class is set once here so CSS rules keyed on
        # it (action padding, tree border-left highlight) take effect from first paint. The
        # focus-driven auto-toggle is retired; ``ActionMenu._set_pane_expanded`` remains as a
        # manual hook if collapse is ever restored.
        self.add_class("-actions-expanded")

    @property
    def view_model(self) -> TopicTreePanelVM:
        return self._vm

    def compose(self):
        yield Static("Topics", id="browser-tree-title")
        with Horizontal(id="browser-tree-body"):
            yield ActionMenu(id="browser-tree-actions")
            yield TopicTree(self._vm.tree, id="topic-tree")
        yield TopicDetails(self._vm.details, id="browser-topic-details")
        yield TopicsDeleteMenu(id="browser-topic-delete")

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
            self.query_one(TopicTree).update_node_label(topic.id, topic.name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Focus-within tracking — inline ``styles.border`` (no CSS class involved)
    # ------------------------------------------------------------------
    #
    # Textual exposes ``on_descendant_focus`` and ``on_descendant_blur``, but neither is a clean signal
    # for "focus entered subtree" or "focus left subtree" — both fire on every focus shift among
    # descendants, including sibling-to-sibling moves where focus stays inside. The asymmetry that
    # matters is in the post-conditions: after a descendant-focus event, focus is *definitely* inside
    # the subtree (the event is its own proof). A descendant-blur is ambiguous, but it doesn't matter
    # here — both handlers funnel into the same ``screen.focused``-derived check.
    #
    # The border color is set via ``self.styles.border`` rather than by toggling a ``-focus-within``
    # CSS class. Inline styles are node-scoped, so Textual doesn't re-evaluate descendant selectors
    # when they change — which avoids the defensive subtree reapply that a class change triggers
    # (every TextArea / Tree / Table in the panel was getting restyled on every focus shift, even
    # though no rule actually keys descendants off ``-focus-within``).

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self._sync_focus_within()

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        self._sync_focus_within()

    def _sync_focus_within(self) -> None:
        focused = self.screen.focused if self.screen else None
        inside = focused is not None and (focused is self or self in focused.ancestors_with_self)
        self.styles.border = ("solid", "#6a6a6a" if inside else "#3a3a3a")

    # ------------------------------------------------------------------
    # ActionMenu message handlers — stubs for now; real dialogs land later.
    # ------------------------------------------------------------------

    def on_action_menu_view_rename_requested(
        self, event: ActionMenu.RenameRequested
    ) -> None:
        # "Rename" is just a shortcut to the name field in the details panel — the buffered-edit
        # flow there already handles the commit. No-ops if no topic is loaded.
        self.action_rename()

    def on_action_menu_view_create_requested(
        self, event: ActionMenu.CreateRequested
    ) -> None:
        self._dispatch_create(self._vm.tree.cursor_topic_id)

    def on_action_menu_view_delete_requested(
        self, event: ActionMenu.DeleteRequested
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
        dialog = self.query_one(TopicsDeleteMenu)
        dialog.prepare_for_show(topic_name)
        self.add_class("-deleting")
        dialog.focus()

    def on_delete_dialog_view_accepted(
        self, event: TopicsDeleteMenu.Accepted
    ) -> None:
        self.run_worker(self._delete_worker(), exclusive=False)

    def on_delete_dialog_view_cancelled(
        self, event: TopicsDeleteMenu.Cancelled
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
            self.query_one(TopicTree).remove_node(topic_id)
        except Exception:
            pass
        self._end_delete()

    def _end_delete(self) -> None:
        self._delete_target_id = None
        self.remove_class("-deleting")
        try:
            self.query_one("#topic-tree").focus()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Alt-arrow nav — mixin handles the graph walk; we just bubble on no-handle so the keystroke
    # can reach ``Browser`` for cross-region handling (e.g., alt+right at the rightmost panel
    # widget hops into the active tab).
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    def _is_node_available(self, node_id: str) -> bool:
        # Tree + actions are always available. The delete dialog only competes for the tree-down
        # slot while the panel is in ``-deleting`` state, and the details fields require a
        # selected topic; ``-choices`` additionally requires a dirty buffer.
        if node_id == "browser-topic-delete":
            return self.has_class("-deleting")
        if node_id in ("topic-details-name", "topic-details-description"):
            return self._vm.details.topic is not None
        if node_id == "topic-details-choices":
            return self._vm.details.is_dirty
        return True

    def action_rename(self) -> None:
        if self._vm.details.topic is None:
            return
        try:
            self.query_one("#topic-details-name").focus()
        except Exception:
            pass

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
            tree_view = self.query_one(TopicTree)
        except Exception:
            return
        await tree_view.add_created_topic(topic)
