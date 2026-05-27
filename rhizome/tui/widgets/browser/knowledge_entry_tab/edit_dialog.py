"""Edit-action picker (``_EditBar``) and the inline ``_TypePickerScreen`` modal it spawns.

The bar sits in the same screen slot as the other dialogs (the tab runs the mutex). The picker
modal is co-located here rather than under ``tui/screens/`` — it's tiny and only used by the bar's
``change type`` dispatch, so the extra indirection isn't worth it. Lift it if a second consumer
appears.
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


# Edit-dialog action choices, ordered left-to-right as shown to the user. ``edit title`` /
# ``edit content`` only appear in single-select mode (they refocus the corresponding details
# TextArea, which has no useful meaning for a bulk edit). Order matters: the destructive ``delete``
# sits last so the cursor never lands on it without an explicit rightward step.
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
    """Modal screen for picking an ``EntryType``. Three options laid out vertically; arrows / enter
    / escape. Dismisses with the chosen ``EntryType`` (caller applies it) or ``None`` on cancel."""

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
        # Land the cursor on the current type when there is one, so the most common "I want to
        # change to something other than this" flow is one ``down`` away.
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
    """Horizontal edit-action picker. Options come from the local constant pair
    (``_EDIT_OPTIONS_SINGLE`` / ``_EDIT_OPTIONS_MULTI``); multi-select hides the per-entry
    edit shortcuts since they have no useful meaning for a bulk edit. All options route
    through ``tab.handle_edit_choice(label)`` — two of them open modal screens, two are pure
    focus shortcuts to the details panel.

    Visual differs from the standard ``ChoiceList`` render: no ``►`` marker, colour-only
    cursor distinction (gold-on-focus / grey-on-blur vs ``#787878`` for non-cursor) so the
    horizontal row stays compact across 3–5 options.
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
        # All labels route through ``_dispatch``, which reads the cursor to recover which one
        # was picked. Acceptable cost for keeping the base widget free of "method-receives-
        # label" plumbing.
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
