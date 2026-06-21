"""BranchPointModel — the feed-resident VM marking a branch point in the conversation.

Lives in the branching node's feed (appended by ``ChatAreaModel.branch`` just before the node
freezes). Displays the branches reachable from that point and, when the cursor has descended through
it, which branch is currently selected. State is push-driven: the chat area walks the cursor path on
every cursor move and calls ``set_selected_child`` directly — no event-pump subscription.

While focused, navigation keys forward to the ``request_*`` methods, which call back into the chat
area to mutate the cursor (``descend`` / ``ascend`` / ``swap_sibling``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhizome.app.model import ViewModelBase

from .conversation_graph import ConversationNode

if TYPE_CHECKING:
    from .chat_area import ChatAreaModel


class BranchPointModel(ViewModelBase):
    """One branch point. Display state derives from ``selected_child``:

    - ``None`` — the cursor sits at the branching node itself (pre-descent).
    - a node — the cursor has descended through that child; render the children with it highlighted.

    Holds the chat area reference purely for navigation callbacks (``request_*``); updates flow the
    other way (the area pushes via ``set_selected_child``).
    """

    def __init__(self, area: "ChatAreaModel", node: ConversationNode) -> None:
        super().__init__()
        self._area = area
        self._node = node
        self._selected_child: ConversationNode | None = None
        self.is_navigable = True
        # Focus state mirrored from the view. Textual sets the widget's ``has_focus`` asynchronously
        # relative to the focus/blur event dispatch, so the view snapshots focus here synchronously
        # (before the dirty emit) and reads this instead during refresh.
        self.is_focused: bool = False

    # ------------------------------------------------------------------
    # Derived state (read by the view)
    # ------------------------------------------------------------------

    @property
    def node(self) -> ConversationNode:
        return self._node

    @property
    def children(self) -> tuple[ConversationNode, ...]:
        """The branch point's children, in creation order."""
        return self._area.conversation_graph.children(self._node)

    @property
    def selected_child(self) -> ConversationNode | None:
        return self._selected_child

    def child_name(self, child: ConversationNode) -> str:
        """Display name for a child; falls back to ``branch-{id}`` when unnamed."""
        return child.name or f"branch-{child.id}"

    @property
    def selected_child_name(self) -> str:
        """Actual stored name of the selected child (empty string if unnamed) — used to pre-fill the
        rename editor so the ``branch-{id}`` fallback never leaks into editable text."""
        if self._selected_child is None:
            return ""
        return self._selected_child.name or ""

    # ------------------------------------------------------------------
    # State updates (pushed by ChatAreaModel on cursor moves)
    # ------------------------------------------------------------------

    def set_selected_child(self, child: ConversationNode | None) -> None:
        """Push the new selected child. Emits ``OnDirty`` only on an actual change, so a broadcast
        walk over many indicators is a quiet no-op for the ones already correct."""
        if child is self._selected_child:
            return
        self._selected_child = child
        self.emit(self.Callbacks.OnDirty)

    def notify_focused(self) -> None:
        if self.is_focused:
            return
        self.is_focused = True
        self.emit(self.Callbacks.OnDirty)

    def notify_blurred(self) -> None:
        if not self.is_focused:
            return
        self.is_focused = False
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Navigation requests (called by the view on keystrokes)
    # ------------------------------------------------------------------

    def request_descend(self) -> None:
        """Descend into the leftmost child. Only meaningful at-point (no descent yet)."""
        if self._selected_child is not None:
            return
        children = self.children
        if not children:
            return
        self._area.descend(children[0])

    def request_ascend(self) -> None:
        """Truncate the cursor so this branch point becomes the leaf — "un-descend out of *this*
        branch point" regardless of how many levels deeper the cursor sits."""
        if self._selected_child is None:
            return
        self._area.ascend(to=self._node)

    def request_sibling(self, direction: int) -> None:
        """Swap the descended child for its left (-1) / right (+1) sibling at this branch point."""
        if self._selected_child is None:
            return
        self._area.swap_sibling(direction, at=self._node)

    def request_rename(self, new_name: str) -> None:
        """Rename the currently-selected child. No-op without a descent (no unambiguous target);
        empty/whitespace input clears the name back to the ``branch-{id}`` fallback."""
        if self._selected_child is None:
            return
        self._area.conversation_graph.rename(self._selected_child, new_name.strip() or None)
        self.emit(self.Callbacks.OnDirty)
