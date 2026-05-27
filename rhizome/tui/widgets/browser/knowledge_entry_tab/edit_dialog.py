"""Edit-action picker (``_EditBar``) and the inline ``_TypePickerScreen`` modal it spawns.

The type picker is co-located rather than living under ``tui/screens/`` because it's tiny and only
used here; lift it if a second consumer appears.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from rhizome.db.models import EntryType

from ..choices import ChoiceList
from .view_model import KnowledgeEntryBrowserTabViewModel

if TYPE_CHECKING:
    from .view import KnowledgeEntryBrowserTabView


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


class _TypePickerScreen(ModalScreen[EntryType | None]):
    """Vertical EntryType picker. Arrows / enter / escape. Dismisses with the chosen type or None."""

    DEFAULT_CSS = """
    _TypePickerScreen {
        align: center middle;
    }
    _TypePickerScreen > Vertical {
        width: 40;
        height: auto;
        border: solid $surface-lighten-2;
        padding: 1 2;
        background: $surface;
    }
    _TypePickerScreen Static {
        color: rgb(150,150,150);
    }
    _TypePickerScreen #type-picker-header {
        margin-bottom: 1;
        color: rgb(100,100,100);
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "select", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, *, current: EntryType | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._options: tuple[EntryType, ...] = tuple(EntryType)
        # Land on the current type so the common "change to something else" flow is one keystroke.
        if current is not None and current in self._options:
            self._cursor = self._options.index(current)
        else:
            self._cursor = 0

    def compose(self):
        with Vertical():
            yield Static(
                "Select entry type  (↑/↓ navigate, enter select, esc cancel)",
                id="type-picker-header",
            )
            yield Static(self._render_options(), id="type-picker-options")

    def _render_options(self) -> Text:
        text = Text()
        for i, opt in enumerate(self._options):
            is_cursor = i == self._cursor
            if is_cursor:
                text.append("► ", style="bold #ffd700")
                text.append(opt.value, style="bold")
            else:
                text.append("  ")
                text.append(opt.value, style="dim")
            if i < len(self._options) - 1:
                text.append("\n")
        return text

    def _repaint(self) -> None:
        self.query_one("#type-picker-options", Static).update(self._render_options())

    def action_cursor_up(self) -> None:
        self._cursor = (self._cursor - 1) % len(self._options)
        self._repaint()

    def action_cursor_down(self) -> None:
        self._cursor = (self._cursor + 1) % len(self._options)
        self._repaint()

    def action_select(self) -> None:
        self.dismiss(self._options[self._cursor])

    def action_cancel(self) -> None:
        self.dismiss(None)


class _EditBar(ChoiceList[KnowledgeEntryBrowserTabViewModel]):
    """Horizontal edit-action picker. Options come from ``_EDIT_OPTIONS_*``; every label
    dispatches through ``tab.handle_edit_choice(label)``.

    Custom per-choice render: no ``►`` marker, colour-only cursor distinction (gold-on-focus,
    grey-on-blur) so the horizontal row stays compact across 3-5 options.
    """

    HINT = "← / → move • enter select • e/esc dismiss"

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(view_model, **kwargs)
        self._tab = tab

    def choices(self) -> dict[str, str]:
        # All labels route through ``_dispatch``, which reads the cursor to recover the label —
        # keeps the base widget free of method-receives-label plumbing.
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
