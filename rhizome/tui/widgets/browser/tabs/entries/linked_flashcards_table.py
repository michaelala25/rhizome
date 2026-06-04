"""``LinkedFlashcardsTable`` — ``DataTable`` with auto-load-more at the bottom edge (pool
pagination during relink) and a ``space`` binding for the relink-set toggle."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable

from rhizome.app.browser.tabs.entries.linked_flashcards import LinkedFlashcardsPanelVM
from rhizome.tui.keybindings import Keybind


class LinkedFlashcardsTable(DataTable):
    BINDINGS = [
        Keybind.Toggle.as_binding("toggle_relink_selection", show=False),
    ]

    def __init__(
        self,
        view_model: LinkedFlashcardsPanelVM,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge. ``vm.load_more`` is a no-op outside
        relink / when no more is available / mid-fetch, so calling at the edge is safe."""
        if (
            self._vm.remaining_has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_toggle_relink_selection(self) -> None:
        self._vm.toggle_current_relink_selection()
