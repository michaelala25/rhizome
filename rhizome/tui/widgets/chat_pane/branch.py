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

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.events import Focus
from textual.message import Message
from textual.widgets import Static

from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.tui.widgets.shared.text_area import ConfirmableTextArea
from rhizome.app.chat_pane.branch import BranchPointVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view
from rhizome.app.chat_pane.conversation_graph import ConversationGraph, NodeId

if TYPE_CHECKING:
    from .view_model import ChatPaneVM


class RenameTextArea(ConfirmableTextArea):
    """One-line ``ConfirmableTextArea`` with vertical accent bars on the left/right sides —
    rendered via ``border-left``/``border-right: tall``, so the focus indicator costs no extra
    widget and stays at a total height of one row.

    ``enter`` and ``ctrl+enter`` both post ``AcceptEditsRequested`` (inherited from the parent);
    ``escape`` posts ``CancelEditsRequested``. ``TextArea`` intercepts ``enter`` (insertion) and
    ``escape`` (focus-next) before the binding system runs, so ``_on_key`` short-circuits both
    and routes them to the corresponding actions.

    Only used by ``BranchPoint`` today, so it lives here rather than in the shared package.
    """

    BINDINGS = [
        Binding("enter", "accept_edits", show=False),
        Binding("escape", "cancel_edits", show=False),
    ]

    DEFAULT_CSS = """
    RenameTextArea {
        width: auto;
        min-width: 20;
        height: 1;
        background: rgb(40, 40, 40);
        border-left: tall rgb(80, 80, 80);
        border-right: tall rgb(80, 80, 80);
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 0;
    }
    RenameTextArea:focus {
        border-left: tall $accent;
        border-right: tall $accent;
    }
    """

    class CancelEditsRequested(Message):
        """User pressed escape inside the rename field."""

    def action_cancel_edits(self) -> None:
        self.post_message(self.CancelEditsRequested())

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_accept_edits()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel_edits()
            return
        await super()._on_key(event)


@register_feed_view(BranchPointVM)
class BranchPoint(NavigableFeedItemViewBase[BranchPointVM]):
    """Bright-grey banner with the navigable-feed-item border (dim → hover → focus). Focusable via
    click; keystrokes only fire when focused so they never compete with the chat input's word-nav."""

    # All four nav keys live on ``alt+<arrow>`` to match the other navigable feed widgets — and to
    # leave ``ctrl+up`` / ``ctrl+down`` free for the chat pane's feed-wide navigation. Up/down are
    # framed as expand/collapse (descend into the leftmost child / pop back to this indicator's
    # parent), which reads more naturally for a branch tree than ascend/descend.
    BINDINGS = [
        Binding("alt+left", "sibling_left", "Sibling left", show=False),
        Binding("alt+right", "sibling_right", "Sibling right", show=False),
        Binding("alt+up", "ascend", "Collapse", show=False),
        Binding("alt+down", "descend", "Expand", show=False),
        Binding("r", "begin_rename", "Rename", show=False),
    ]

    # Override the navigable-base border so only the top/bottom edges draw — the bare horizontal
    # rules span the full chat-area width, matching the pre-MVVM banner look. Hover/focus colour
    # swaps mirror the base's three-tier palette.
    DEFAULT_CSS = """
    BranchPoint {
        border: none;
        border-top: solid rgb(60, 60, 60);
        border-bottom: solid rgb(60, 60, 60);
        height: auto;
        padding: 0 1;
        margin: 1 0;
        color: rgb(180, 180, 180);
    }
    BranchPoint:hover {
        border-left: none;
        border-right: none;
        border-top: solid rgb(120, 120, 120);
        border-bottom: solid rgb(120, 120, 120);
    }
    BranchPoint:focus, BranchPoint:focus-within {
        border-left: none;
        border-right: none;
        border-top: solid rgb(90, 140, 200);
        border-bottom: solid rgb(90, 140, 200);
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
    BranchPoint RenameTextArea {
        margin: 0 0 0 1;
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
        self._rename_editor: RenameTextArea | None = None
        # View-side rename mode: drives the editor's visibility and the hint text. Lives on the
        # widget rather than the VM because it's purely UI state — the VM only cares about the
        # final rename request, not whether an editor is currently open.
        self._renaming = False

    def compose(self) -> ComposeResult:
        # Branches static (left, auto), hint static (1fr, right-aligned), and the bracketed rename
        # editor (right, auto, hidden until ctrl+r). The editor handles its own brackets and
        # focus-aware styling internally — see ``RenameTextArea``.
        self._branches_static = Static(self._render_branches(), markup=True, classes="branches")
        self._hint_static = Static(self._render_hint(), markup=True, classes="hint")
        # ``compact=True`` strips the default padding + scrollbar row; ``soft_wrap=False`` keeps
        # overflow on a single line so the cursor pans horizontally instead of wrapping.
        self._rename_editor = RenameTextArea(compact=True, soft_wrap=False)
        self._rename_editor.display = False
        with Horizontal():
            yield self._branches_static
            yield self._hint_static
            yield self._rename_editor

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

    # Key/label colours match the entry tab's hint row in the browser — slightly brighter grey for
    # the key, dimmer grey for the action label, three spaces between pairs. The ``_DIM_*`` pair
    # is used when the indicator is blurred so the hints fade enough to signal "not currently
    # actionable" without disappearing — we deliberately keep them visible (no "click to focus"
    # fallback) because the app is steering away from mouse input.
    _HINT_KEY_COLOR = "#a0a0a0"
    _HINT_LABEL_COLOR = "#707070"
    _HINT_KEY_COLOR_DIM = "#555555"
    _HINT_LABEL_COLOR_DIM = "#404040"

    @classmethod
    def _format_hints(cls, pairs: list[tuple[str, str]], *, dim: bool = False) -> str:
        key_color = cls._HINT_KEY_COLOR_DIM if dim else cls._HINT_KEY_COLOR
        label_color = cls._HINT_LABEL_COLOR_DIM if dim else cls._HINT_LABEL_COLOR
        return "   ".join(
            f"[{key_color}]{key}[/] [{label_color}]{label}[/]" for key, label in pairs
        )

    def _render_hint(self) -> str:
        if self._renaming:
            return self._format_hints([("enter", "confirm"), ("esc", "cancel")])
        pairs = (
            [("alt+↓", "expand")]
            if self._vm.selected_child is None
            else [("alt+←/→", "navigate"), ("alt+↑", "collapse"), ("r", "rename")]
        )
        return self._format_hints(pairs, dim=not self._vm.is_focused)

    # ------------------------------------------------------------------
    # Action handlers (forward to the VM)
    # ------------------------------------------------------------------

    def on_focus(self, event: Focus) -> None:
        # If focus lands back on the indicator while a rename is active (e.g. the user tabbed away
        # and back), forward it straight to the editor — otherwise the editor stays unreachable
        # from keyboard until the user clicks it.
        super().on_focus(event)
        if self._renaming and self._rename_editor is not None:
            self._rename_editor.focus()

    # All four navigation actions no-op while a rename is in progress. ``TextArea`` doesn't bind
    # any of the ``alt+<arrow>`` combos, so every nav key bubbles straight up from the editor —
    # without these guards, swapping the selected child mid-rename would silently change the
    # target of the pending name.

    def action_sibling_left(self) -> None:
        if self._renaming:
            return
        self._vm.request_sibling(-1)

    def action_sibling_right(self) -> None:
        if self._renaming:
            return
        self._vm.request_sibling(1)

    def action_ascend(self) -> None:
        if self._renaming:
            return
        self._vm.request_ascend()

    def action_descend(self) -> None:
        if self._renaming:
            return
        self._vm.request_descend()

    def action_begin_rename(self) -> None:
        # Guard: ctrl+r is only meaningful when a child is selected (without descent, there's no
        # unambiguous target). Also no-op if the editor is already open — re-pressing ctrl+r while
        # editing would otherwise blow away typed input.
        if self._vm.selected_child is None or self._renaming or self._rename_editor is None:
            return
        self._renaming = True
        self._rename_editor.text = self._vm.selected_child_name
        self._rename_editor.display = True
        self._rename_editor.focus()
        # Refresh the hint immediately. The VM hasn't changed, so a full ``_refresh`` would be
        # wasteful — just repaint the hint static to reflect the new mode.
        if self._hint_static is not None:
            self._hint_static.update(self._render_hint())

    def on_confirmable_text_area_accept_edits_requested(
        self, message: ConfirmableTextArea.AcceptEditsRequested
    ) -> None:
        if not self._renaming or self._rename_editor is None:
            return
        message.stop()
        new_name = self._rename_editor.text
        self._end_rename()
        self._vm.request_rename(new_name)

    def on_rename_text_area_cancel_edits_requested(
        self, message: RenameTextArea.CancelEditsRequested
    ) -> None:
        if not self._renaming:
            return
        message.stop()
        self._end_rename()

    def _end_rename(self) -> None:
        """Tear down rename mode and return focus to the indicator. The subsequent
        ``on_focus`` → ``notify_focused`` → ``dirty`` chain triggers a full ``_refresh``, which
        repaints the hint with the normal navigation text — so no manual hint update needed here.
        """
        self._renaming = False
        if self._rename_editor is not None:
            self._rename_editor.display = False
        self.focus()
