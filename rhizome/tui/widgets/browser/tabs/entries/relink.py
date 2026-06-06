"""``RelinkMenu`` — Accept / Cancel choices widget for a pending flashcard-relink. Visibility is
driven by ``vm.is_relink_dirty`` via a ``.-visible`` class toggle in the parent's ``_refresh``.

The widget is mounted once and survives multiple show/hide cycles, so its cursor persists
across them (no ``prepare_for_show`` reset). Escape always cancels regardless of cursor.
"""

from __future__ import annotations

from rhizome.app.browser.tabs.entries.linked_flashcards import LinkedFlashcardsPanelModel
from rhizome.tui.widgets.shared.choices_list import ChoiceList


class RelinkMenu(ChoiceList[LinkedFlashcardsPanelModel]):
    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Relink: "
    HINT = "← / → move • enter confirm • esc cancels"

    async def _accept(self) -> None:
        await self._vm.accept_relink()

    def _cancel(self) -> None:
        self._vm.cancel_relink()

    def action_cancel(self) -> None:
        self._vm.cancel_relink()
