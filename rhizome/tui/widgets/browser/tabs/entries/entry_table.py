"""``EntryTable`` — entries-tab DataTable. Inherits ``space`` / ``shift+up`` / ``shift+down``
from the base multi-select mixin; adds auto-load-more on cursor-down at the bottom edge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rhizome.app.browser.tabs.entries.tab import EntryTabVM
from rhizome.tui.widgets.browser.shared.multiselect_table import MultiSelectableDataTable

if TYPE_CHECKING:
    from rhizome.tui.widgets.browser.tabs.entries.tab import EntryTab


class EntryTable(MultiSelectableDataTable[EntryTabVM]):
    def __init__(
        self,
        view_model: EntryTabVM,
        tab: "EntryTab",
        **kwargs: Any,
    ) -> None:
        super().__init__(view_model, **kwargs)
        self._tab = tab

    async def action_cursor_down(self) -> None:
        # Await ``load_more`` first (no-op if nothing available / fetch in flight) so the
        # subsequent ``super().action_cursor_down`` has a fresh row to land on. By the time the
        # await returns, ``_refresh`` has run in ``extend`` mode and the rows are mounted.
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()
