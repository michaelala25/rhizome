"""BranchIndicator — sub-VM + view representing a /branch point in the chat feed.

Lives in the parent node's feed (appended by ``ChatPaneViewModel.branch()`` at the moment of /branch).
Displays the branches reachable from that point and, when the cursor has descended through it, which
branch is currently selected. State is push-driven: the chat pane walks the visible feed on every
cursor move and calls ``set_selected_child(...)`` directly — no event-pump subscription.

The widget is focusable. While focused, ctrl+arrow keys forward to the VM, which calls back into
the chat pane VM to mutate the cursor (``descend_into`` / ``ascend`` / ``swap_sibling``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static

from ..view_base import ViewBase
from ..view_model_base import ViewModelBase
from .conversation_graph import ConversationGraph, NodeId

if TYPE_CHECKING:
    from .view_model import ChatPaneViewModel


class BranchIndicatorViewModel(ViewModelBase):
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
        chat_pane: "ChatPaneViewModel",
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

    # ------------------------------------------------------------------
    # State updates (called by ChatPaneViewModel on cursor moves)
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
        ``ChatPaneViewModel.ascend``.
        """
        if self._selected_child is None:
            return
        self._chat_pane.ascend(parent_node_id=self._parent_node_id)

    def request_sibling(self, direction: int) -> None:
        """ctrl+left (-1) / ctrl+right (+1): swap horizontal sibling at *this* branch point.

        Passes ``parent_node_id`` so the swap happens at this indicator's level even if the cursor
        currently sits several levels deeper. See ``ChatPaneViewModel.swap_sibling`` for the
        truncation semantics.
        """
        if self._selected_child is None:
            return
        self._chat_pane.swap_sibling(direction, parent_node_id=self._parent_node_id)


class BranchIndicatorView(ViewBase[BranchIndicatorViewModel]):
    """Bright-grey banner with top/bottom borders. Focusable via click; keystrokes only fire when
    focused so they never compete with the chat input's word-nav."""

    can_focus = True

    # ctrl+up / ctrl+down are reserved by the chat pane for feed-wide navigation; ascend/descend
    # therefore live on alt+up / alt+down. TODO: the asymmetry with sibling-swap on ctrl+left/right is
    # a wart — consider moving all four to alt+arrows for consistency once the feed-nav UX settles.
    BINDINGS = [
        Binding("ctrl+left", "sibling_left", "Sibling left", show=False),
        Binding("ctrl+right", "sibling_right", "Sibling right", show=False),
        Binding("alt+up", "ascend", "Ascend", show=False),
        Binding("alt+down", "descend", "Descend", show=False),
    ]

    DEFAULT_CSS = """
    BranchIndicatorView {
        height: auto;
        padding: 0 1;
        margin: 1 0;
        border-top: heavy rgb(120, 120, 120);
        border-bottom: heavy rgb(120, 120, 120);
        color: rgb(180, 180, 180);
    }
    BranchIndicatorView:focus {
        border-top: heavy rgb(220, 220, 220);
        border-bottom: heavy rgb(220, 220, 220);
        color: rgb(220, 220, 220);
    }
    BranchIndicatorView Horizontal {
        height: auto;
        width: 1fr;
    }
    BranchIndicatorView .branches {
        width: auto;
        height: auto;
    }
    BranchIndicatorView .hint {
        width: 1fr;
        height: auto;
        content-align-horizontal: right;
    }
    """

    # Number of siblings shown on each side of the selected branch. Remaining siblings on
    # either side collapse into a "+N more" marker so a branch point with many children
    # doesn't blow out the indicator's width.
    _VISIBLE_SIBLINGS_PER_SIDE = 2

    def __init__(self, vm: BranchIndicatorViewModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._branches_static: Static | None = None
        self._hint_static: Static | None = None

    def compose(self) -> ComposeResult:
        # Split into two Statics inside a Horizontal so the hint can sit flush right (via
        # ``content-align-horizontal: right`` on the .hint static taking the remaining 1fr).
        # Both render paths are independent, but we drive them from a single ``_refresh`` for
        # simplicity — the strings are short, repaint cost is negligible.
        self._branches_static = Static(self._render_branches(), markup=True, classes="branches")
        self._hint_static = Static(self._render_hint(), markup=True, classes="hint")
        with Horizontal():
            yield self._branches_static
            yield self._hint_static

    def _refresh(self) -> None:
        # Pre-compose dirties (fired by set_selected_child / notify_focused before mount
        # finishes) are no-ops by design — compose will read the current VM state when it
        # eventually runs.
        if self._branches_static is not None:
            self._branches_static.update(self._render_branches())
        if self._hint_static is not None:
            self._hint_static.update(self._render_hint())

    def _render_branches(self) -> str:
        children = self._vm.children
        selected = self._vm.selected_child

        if selected is None:
            n = len(children)
            suffix = "es" if n != 1 else ""
            return f"▼ {n} branch{suffix} below"

        try:
            idx = children.index(selected)
        except ValueError:
            # Defensive: indicator's selected_child isn't one of its children. Shouldn't happen
            # under normal operation but render something readable instead of crashing.
            return f"(detached selection: {self._vm.child_name(selected)})"

        # Window the displayed siblings around the selected one so wide branch points stay
        # readable. Anything outside the window collapses to a "+N more" marker on that side.
        lo = max(0, idx - self._VISIBLE_SIBLINGS_PER_SIDE)
        hi = min(len(children), idx + self._VISIBLE_SIBLINGS_PER_SIDE + 1)

        parts: list[str] = []
        if lo > 0:
            parts.append(f"[dim]+{lo} more[/]")
        for i in range(lo, hi):
            name = self._vm.child_name(children[i])
            if i == idx:
                parts.append(f"[bold]● {name}[/]")
            else:
                parts.append(f"[dim]{name}[/]")
        if hi < len(children):
            parts.append(f"[dim]+{len(children) - hi} more[/]")

        return " [dim]/[/] ".join(parts)

    def _render_hint(self) -> str:
        focused = self._vm.is_focused
        if self._vm.selected_child is None:
            action_hint = "alt+↓ to descend"
        else:
            action_hint = "ctrl+←/→ to navigate"
        return f"[dim]{action_hint if focused else 'click to focus'}[/]"

    # ------------------------------------------------------------------
    # Action handlers (forward to the VM)
    # ------------------------------------------------------------------

    def action_sibling_left(self) -> None:
        self._vm.request_sibling(-1)

    def action_sibling_right(self) -> None:
        self._vm.request_sibling(1)

    def action_ascend(self) -> None:
        self._vm.request_ascend()

    def action_descend(self) -> None:
        self._vm.request_descend()
