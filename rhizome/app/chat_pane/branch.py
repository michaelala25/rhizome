"""BranchIndicator — sub-VM + view representing a /branch point in the chat feed.

Lives in the parent node's feed (appended by ``ChatPaneModel.branch()`` at the moment of /branch).
Displays the branches reachable from that point and, when the cursor has descended through it, which
branch is currently selected. State is push-driven: the chat pane walks the visible feed on every
cursor move and calls ``set_selected_child(...)`` directly — no event-pump subscription.

The widget is focusable. While focused, ``alt+<arrow>`` keys forward to the VM, which calls back
into the chat pane VM to mutate the cursor (``descend_into`` / ``ascend`` / ``swap_sibling``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static

from rhizome.app.model import ViewModelBase
from rhizome.app.chat_pane.conversation_graph import ConversationGraph, NodeId

if TYPE_CHECKING:
    from .view_model import ChatPaneModel


class BranchPointModel(ViewModelBase):
    """Represents a single branch point. Display state is derived from ``_selected_child``:

    - ``None`` — cursor is at the parent node (pre-descent). Renders "N branches below ...".
    - a ``NodeId`` — cursor has descended through that child. Renders all children with the
      descended one highlighted, plus left/right hints when more siblings exist that direction.

    Holds a reference to the chat pane VM purely for navigation callbacks (``request_*``). No
    subscription to the pane is set up; updates flow the other way (pane pushes via
    ``set_selected_child``).
    """

    def __init__(
        self,
        graph: ConversationGraph,
        parent_node_id: NodeId,
        chat_pane: "ChatPaneModel",
    ) -> None:
        super().__init__()
        self._graph = graph
        self._parent_node_id = parent_node_id
        self._chat_pane = chat_pane
        self._selected_child: NodeId | None = None
        self.is_navigable = True
        # Focus state mirrored from the view. The view-side ``has_focus`` is set asynchronously
        # by Textual relative to the focus/blur event dispatch, so reading it inside the
        # ensuing ``_refresh`` can return stale values. We snapshot focus into the VM
        # *synchronously* inside ``notify_focused`` / ``notify_blurred`` (before the dirty
        # emit), so the view reads a consistent value.
        self.is_focused: bool = False

    # ------------------------------------------------------------------
    # Derived state (read by the view)
    # ------------------------------------------------------------------

    @property
    def parent_node_id(self) -> NodeId:
        return self._parent_node_id

    @property
    def children(self) -> tuple[NodeId, ...]:
        """Children of the branch point, in left-to-right horizontal order."""
        return self._graph.children(self._parent_node_id)

    @property
    def selected_child(self) -> NodeId | None:
        return self._selected_child

    def child_name(self, child_id: NodeId) -> str:
        """Display name for a child; falls back to ``branch-{id}`` when unnamed."""
        name = self._graph.node(child_id).name
        return name if name else f"branch-{child_id}"

    @property
    def selected_child_name(self) -> str:
        """Actual stored name of the selected child (empty string if unnamed). Distinct from
        ``child_name(...)`` — the view uses this to pre-fill the rename editor so the fallback
        ``branch-{id}`` placeholder never leaks into the editable text."""
        if self._selected_child is None:
            return ""
        return self._graph.node(self._selected_child).name or ""

    # ------------------------------------------------------------------
    # State updates (called by ChatPaneModel on cursor moves)
    # ------------------------------------------------------------------

    def set_selected_child(self, child_id: NodeId | None) -> None:
        """Push the new selected child. Emits ``dirty`` only when the value actually changes,
        so a broadcast walk over many indicators is a quiet no-op for the ones already correct.
        """
        if child_id == self._selected_child:
            return
        self._selected_child = child_id
        self.emit(self.dirty)

    def notify_focused(self) -> None:
        """View-side focus arrival. Mirror to ``is_focused`` *before* the dirty emit so the
        ensuing ``_refresh`` reads a consistent state — Textual's ``has_focus`` is updated
        asynchronously relative to the focus event dispatch and can lag behind the refresh.
        """
        if self.is_focused:
            return
        self.is_focused = True
        self.emit(self.dirty)

    def notify_blurred(self) -> None:
        if not self.is_focused:
            return
        self.is_focused = False
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Navigation requests (called by the view on keystrokes)
    # ------------------------------------------------------------------

    def request_descend(self) -> None:
        """alt+down: descend into the leftmost child. Only meaningful at-point."""
        if self._selected_child is not None:
            return
        children = self.children
        if not children:
            return
        self._chat_pane.descend_into(children[0])

    def request_ascend(self) -> None:
        """alt+up: truncate the cursor to this indicator's parent. Only meaningful when descended.

        Passes ``parent_node_id`` so an ancestor indicator (higher in the path) ascends out of its
        own branch point rather than just popping one level from the leaf — see
        ``ChatPaneModel.ascend``.
        """
        if self._selected_child is None:
            return
        self._chat_pane.ascend(parent_node_id=self._parent_node_id)

    def request_rename(self, new_name: str) -> None:
        """Rename the currently-selected child branch. No-op when no child is selected — without
        a descent there's no unambiguous target. Empty/whitespace input clears the name so the
        indicator falls back to the ``branch-{id}`` display.
        """
        if self._selected_child is None:
            return
        name = new_name.strip() or None
        self._graph.rename(self._selected_child, name)
        self.emit(self.dirty)

    def request_sibling(self, direction: int) -> None:
        """alt+left (-1) / alt+right (+1): swap horizontal sibling at *this* branch point.

        Passes ``parent_node_id`` so the swap happens at this indicator's level even if the cursor
        currently sits several levels deeper. See ``ChatPaneModel.swap_sibling`` for the
        truncation semantics.
        """
        if self._selected_child is None:
            return
        self._chat_pane.swap_sibling(direction, parent_node_id=self._parent_node_id)
