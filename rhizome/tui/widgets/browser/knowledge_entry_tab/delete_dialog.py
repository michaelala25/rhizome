"""Delete confirmation dialog. Targets are whatever ``vm.delete_selected_entries`` resolves
(live selection in multi-select; cursor entry in single-select)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..choices import ChoiceList
from .view_model import EntryTabVM

if TYPE_CHECKING:
    from .view import EntryTab


class EntriesDeleteMenu(ChoiceList[EntryTabVM]):
    """Vertical Confirm/Cancel. Header surfaces the count + the no-flashcards-harmed note."""

    CHOICES = {"Confirm": "_confirm", "Cancel": "_cancel"}
    ORIENTATION = "vertical"

    def __init__(
        self,
        view_model: EntryTabVM,
        tab: "EntryTab",
        **kwargs: Any,
    ) -> None:
        super().__init__(view_model, **kwargs)
        self._tab = tab

    async def _confirm(self) -> None:
        await self._vm.delete_selected_entries()
        self._tab.hide_dialog()

    def _cancel(self) -> None:
        self._tab.hide_dialog()

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def _render_header(self) -> Text | None:
        count = self._tab.selection_target_count()
        noun = "entry" if count == 1 else "entries"
        # Drop "selected" in single-select — reads weird with no visible selection mark.
        scope_word = "selected " if self._vm.multi_select_active else ""
        text = Text()
        text.append(f"Delete {count} {scope_word}{noun}? ", style="bold")
        text.append("Linked flashcards will not be affected.", style="dim")
        return text
