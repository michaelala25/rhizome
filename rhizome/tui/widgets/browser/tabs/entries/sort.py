"""``EntriesSortMenu`` — sort dialog for the entries tab. Concretises the generic ``SortMenu``
base with an inline multi-select warning line."""

from __future__ import annotations

from rich.text import Text

from rhizome.app.browser.tabs.entries.tab import EntryTabModel
from rhizome.tui.widgets.shared.sort_menu import SortMenu


class EntriesSortMenu(SortMenu[EntryTabModel]):
    """Surfaces an inline "Applying clears your selection." warning while multi-select is on."""

    def _extra_hint(self) -> Text | None:
        if self._vm.multi_select_active:
            return Text("Applying clears your selection.", style="#ff8787")
        return None
