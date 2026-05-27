"""Delete confirmation dialog. Targets the multi-select selection or the cursor entry depending on
mode (the VM's ``delete_selected_entries`` resolves this internally)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..choices import ChoiceList
from .view_model import KnowledgeEntryBrowserTabViewModel

if TYPE_CHECKING:
    from .view import KnowledgeEntryBrowserTabView


class _DeleteConfirm(ChoiceList[KnowledgeEntryBrowserTabViewModel]):
    """Two stacked choices (Confirm / Cancel) under a one-line header describing the action
    (entry count + the no-flashcards-harmed promise). Cursor brightness tracks focus."""

    CHOICES = {"Confirm": "_confirm", "Cancel": "_cancel"}
    ORIENTATION = "vertical"

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
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
        # In single-select mode the lead-in is just "Delete this entry?" — "selected" reads
        # weird when there's no visible selection mark. Multi-select keeps the existing
        # phrasing.
        scope_word = "selected " if self._vm.multi_select_active else ""
        text = Text()
        text.append(f"Delete {count} {scope_word}{noun}? ", style="bold")
        text.append("Linked flashcards will not be affected.", style="dim")
        return text
