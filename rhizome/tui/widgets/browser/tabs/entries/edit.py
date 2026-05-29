"""``EditMenu`` — horizontal edit-action picker for the entries tab.

Choices come from ``_EDIT_OPTIONS_*`` (the multi-select set drops single-target-only actions like
"edit title"); every label dispatches through ``tab.handle_edit_choice(label)``. The cursor's
gold-on-focus / grey-on-blur tint keeps the row compact across 3-5 options.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from rhizome.app.browser.tabs.entries.tab import EntryTabVM
from rhizome.tui.widgets.shared.choices_list import ChoiceList

if TYPE_CHECKING:
    from rhizome.tui.widgets.browser.tabs.entries.tab import EntryTab


# Edit-bar choices, ordered left-to-right. ``edit title`` / ``edit content`` are single-select
# only (no useful "the" entry to focus into in multi-select). ``delete`` sits last so the cursor
# never lands on it without an explicit rightward step.
_EDIT_OPTIONS_SINGLE: tuple[str, ...] = (
    "change topic",
    "change type",
    "edit title",
    "edit content",
    "delete",
)
_EDIT_OPTIONS_MULTI: tuple[str, ...] = (
    "change topic",
    "change type",
    "delete",
)


class EditMenu(ChoiceList[EntryTabVM]):
    """Horizontal edit-action picker. All labels route through ``_dispatch`` → the tab's handler."""

    HINT = "← / → move • enter select • e/esc dismiss"

    def __init__(
        self,
        view_model: EntryTabVM,
        tab: "EntryTab",
        **kwargs: Any,
    ) -> None:
        super().__init__(view_model, **kwargs)
        self._tab = tab

    def choices(self) -> dict[str, str]:
        options = (
            _EDIT_OPTIONS_MULTI
            if self._vm.multi_select_active
            else _EDIT_OPTIONS_SINGLE
        )
        return {opt: "_dispatch" for opt in options}

    async def _dispatch(self) -> None:
        labels = list(self.choices().keys())
        if self._cursor < len(labels):
            await self._tab.handle_edit_choice(labels[self._cursor])

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def _render_lead(self) -> Text | None:
        count = self._tab.selection_target_count()
        if self._vm.multi_select_active:
            noun = "entry" if count == 1 else "entries"
            return Text(f"edit {count} {noun}:  ", style="dim")
        return Text("edit this entry:  ", style="dim")

    def _render_choice(self, label: str, selected: bool) -> Text:
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        style = cursor_color if selected else "#787878"
        return Text(label, style=style)
