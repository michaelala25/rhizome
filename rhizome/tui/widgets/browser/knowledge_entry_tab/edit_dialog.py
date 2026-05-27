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


class _EditBar(Static, can_focus=True):
    """Renders horizontally: option list on one line, hint on the next. Options come from a local
    constant pair (``_EDIT_OPTIONS_SINGLE`` / ``_EDIT_OPTIONS_MULTI``); multi-select hides the
    per-entry edit shortcuts since they have no useful meaning for a bulk edit.

    Keys: ``left`` / ``right`` move the cursor (wrap); ``enter`` dispatches the highlighted choice;
    ``e`` / ``escape`` dismiss; ``s`` / ``f`` / ``d`` swap to the corresponding sibling dialog.

    Dispatch sits on the tab (``handle_edit_choice``) because two of the choices — ``change topic``
    and ``change type`` — open modal screens, and the other two (``edit title`` / ``edit content``)
    are pure focus shortcuts to the details panel. The bar's job is to forward the highlighted
    choice string.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "select", show=False),
        # ``e`` toggles the dialog closed.
        Binding("e", "cancel", show=False),
        Binding("escape", "cancel", show=False),
        Binding("s", "swap_to('sort')", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("d", "swap_to('delete')", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._tab = tab
        # Cursor index into the active options list (mode-dependent).
        self._cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        self._cursor = 0

    def _options(self) -> tuple[str, ...]:
        return (
            _EDIT_OPTIONS_MULTI
            if self._vm.multi_select_active
            else _EDIT_OPTIONS_SINGLE
        )

    def _refresh(self) -> None:
        # Clamp cursor in case the options list shrank under us (e.g. multi-select toggled on while
        # the dialog was open and the cursor was on a single-only option).
        opts = self._options()
        if opts and self._cursor >= len(opts):
            self._cursor = len(opts) - 1
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        # Lead-in: "edit N entries:" / "edit this entry:" — gives the user a clear scope reminder
        # while they navigate.
        count = self._tab.selection_target_count()
        if self._vm.multi_select_active:
            noun = "entry" if count == 1 else "entries"
            text.append(f"edit {count} {noun}:  ", style="dim")
        else:
            text.append("edit this entry:  ", style="dim")
        options = self._options()
        for i, opt in enumerate(options):
            is_cursor = i == self._cursor
            style = cursor_color if is_cursor else "#787878"
            text.append(opt, style=style)
            if i < len(options) - 1:
                text.append("   ")
        text.append("\n")
        text.append("← / → move • enter select • e/esc dismiss", style="dim")
        return text

    def action_cursor_left(self) -> None:
        opts = self._options()
        if opts:
            self._cursor = (self._cursor - 1) % len(opts)
            self._refresh()

    def action_cursor_right(self) -> None:
        opts = self._options()
        if opts:
            self._cursor = (self._cursor + 1) % len(opts)
            self._refresh()

    async def action_select(self) -> None:
        """Forward the highlighted choice string to the tab for dispatch."""
        opts = self._options()
        if not opts or self._cursor < 0 or self._cursor >= len(opts):
            return
        await self._tab.handle_edit_choice(opts[self._cursor])

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._tab.toggle_dialog(name)  # type: ignore[arg-type]
