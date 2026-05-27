"""Search box mounted above the entries table.

Visually mirrors the entry-detail title field: 3-row tight box, transparent background, ``#3a3a3a``
border that flips accent on focus. The keybinding hint rides the top border on the right
(``border_title`` + ``border_title_align = "right"``) — same space the dialogs use for their hint
lines, but here we save a full row by fusing it into the border.

Input state:
  * ``enter`` — submit current buffer to ``vm.set_search``.
  * ``esc`` × 2 — clear buffer + submit empty query (reset). The first esc arms; the second clears.
    Any non-``esc`` key disarms, so a stray esc followed by editing doesn't leave the next esc as a
    surprise nuke.

The state machine lives here (rather than on a parent wrapper) because ``Input`` consumes character
keystrokes before they bubble, so a parent ``on_key`` would never see "user typed something" — the
signal we need to disarm.
"""

from __future__ import annotations

from typing import Any

from textual.binding import Binding
from textual.widgets import Input

from .view_model import KnowledgeEntryBrowserTabViewModel


class _SearchInput(Input):
    DEFAULT_CSS = """
    _SearchInput {
        background: transparent;
        border: solid #3a3a3a;
        height: 3;
        padding: 0 1;
    }
    _SearchInput:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "handle_escape", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self.armed_for_clear: bool = False
        # Border-title hint mounted to the right of the top border, mirroring how IDE search boxes
        # surface their keyboard hints at the edge of the box rather than in a separate row.
        self.border_title_align = "right"
        self._refresh_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Submit handler lives on the input itself rather than the tab view so the search bar is
        # self-contained — the only outward coupling is the ``vm.set_search`` call.
        if event.input is not self:
            return
        self._vm.set_search(event.value)

    def action_handle_escape(self) -> None:
        if self.armed_for_clear:
            self.value = ""
            self._vm.set_search("")
            self.armed_for_clear = False
        else:
            self.armed_for_clear = True
        self._refresh_title()

    def on_key(self, event) -> None:
        """Disarm on any non-escape key. Runs before the binding dispatch (so escape's own action
        still fires) and before ``Input``'s default character-insertion handling (so editing still
        works untouched)."""
        if event.key != "escape" and self.armed_for_clear:
            self.armed_for_clear = False
            self._refresh_title()

    def _refresh_title(self) -> None:
        if self.armed_for_clear:
            self.border_title = "[bold #ff8787]press esc again to clear[/]"
        else:
            self.border_title = "[dim]enter to submit • esc × 2 to clear[/]"
