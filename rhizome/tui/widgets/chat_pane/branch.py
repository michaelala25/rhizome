"""BranchIndicator — sub-VM + view representing a /branch point in the chat feed.

Lives in the parent node's feed (appended by ``ChatPaneVM.branch()`` at the moment of /branch).
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

from rhizome.tui.widgets.navigable_feed_item_view_base import NavigableFeedItemViewBase
from rhizome.app.chat_pane.branch import BranchPointVM
from rhizome.app.chat_pane.conversation_graph import ConversationGraph, NodeId

if TYPE_CHECKING:
    from .view_model import ChatPaneVM


class BranchPoint(NavigableFeedItemViewBase[BranchPointVM]):
    """Bright-grey banner with the navigable-feed-item border (dim → hover → focus). Focusable via
    click; keystrokes only fire when focused so they never compete with the chat input's word-nav."""

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
    BranchPoint {
        height: auto;
        padding: 0 1;
        margin: 1 0;
        color: rgb(180, 180, 180);
    }
    BranchPoint:focus {
        color: rgb(220, 220, 220);
    }
    BranchPoint Horizontal {
        height: auto;
        width: 1fr;
    }
    BranchPoint .branches {
        width: auto;
        height: auto;
    }
    BranchPoint .hint {
        width: 1fr;
        height: auto;
        content-align-horizontal: right;
    }
    """

    # Number of siblings shown on each side of the selected branch. Remaining siblings on
    # either side collapse into a "+N more" marker so a branch point with many children
    # doesn't blow out the indicator's width.
    _VISIBLE_SIBLINGS_PER_SIDE = 2

    def __init__(self, vm: BranchPointVM, **kwargs) -> None:
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
