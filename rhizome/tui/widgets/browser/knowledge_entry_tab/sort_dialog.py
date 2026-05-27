"""Sort-axis picker dialog. Sits in the same screen slot as the other dialogs (the tab runs the
mutex)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from rhizome.db.operations import EntrySortKey

from .view_model import KnowledgeEntryBrowserTabViewModel

if TYPE_CHECKING:
    from .view import KnowledgeEntryBrowserTabView


# Sort axes the dialog surfaces. Ordered left-to-right the way they're laid out (matches the data
# table's column order). The DB op accepts a wider set; the dialog deliberately surfaces the four
# most useful axes.
_SORT_OPTIONS: tuple[EntrySortKey, ...] = ("id", "title", "type", "topic")


class _SortBar(Static, can_focus=True):
    """Renders horizontally, mirroring the data table's column order: ``id   title   type   topic``.
    The active sort is decorated with an arrow (``↑`` / ``↓``) and brackets; the cursor option is
    shown in a bold accent colour (no ``►`` prefix — keeping the row at a fixed width avoids labels
    jumping around as the cursor moves). A second line carries a help hint, extended with a
    selection-clearing warning while multi-select is on.

    Keys: ``left`` / ``right`` move the cursor (with wrap); ``enter`` applies (toggles direction
    when on the active axis, otherwise switches to that axis ascending); ``s`` and ``escape``
    dismiss without applying.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "apply", show=False),
        # ``r`` resets to the default sort (``id`` ascending). Mirrors the same key in the filter
        # dialog.
        Binding("r", "reset", show=False),
        # ``s`` toggles the dialog closed — symmetric with the ``s``-opens-it binding on the
        # entries table.
        Binding("s", "cancel", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("e", "swap_to('edit')", show=False),
        Binding("escape", "cancel", show=False),
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
        # Cursor index into ``_SORT_OPTIONS``. Landed on the currently-active sort axis at
        # ``prepare_for_show``; moves with ←/→.
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
        """Land the cursor on the currently-active sort axis so the most common action (toggle the
        direction of the active sort) is one ``enter`` away. Falls back to ``id`` (index 0) if the
        active axis isn't surfaced in the dialog (e.g. legacy ``created_at`` from an older
        session)."""
        try:
            self._cursor = _SORT_OPTIONS.index(self._vm.sort_by)
        except ValueError:
            self._cursor = 0

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        active_idx = (
            _SORT_OPTIONS.index(self._vm.sort_by)
            if self._vm.sort_by in _SORT_OPTIONS
            else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Cursor colour: bright gold on focus, dim grey otherwise. The active axis itself always
        # renders in the default fg so the arrow + brackets carry the "this is the live sort"
        # signal.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(_SORT_OPTIONS):
            is_active = i == active_idx
            is_cursor = i == self._cursor
            label = f"{arrow}[{option}]" if is_active else option
            if is_cursor:
                style = cursor_color
            elif is_active:
                style = ""  # default fg
            else:
                style = "#787878"
            text.append(label, style=style)
            if i < len(_SORT_OPTIONS) - 1:
                text.append("   ")
        text.append("\n")

        # Help line — extended with the selection-clearing warning when in multi-select. The
        # warning sits inline rather than on a third line so the dialog stays at a fixed 4-line
        # height across both modes.
        hint = Text()
        hint.append(
            "← / → move • enter apply • r reset • s/esc dismiss", style="dim",
        )
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Applying clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def action_cursor_left(self) -> None:
        self._cursor = (self._cursor - 1) % len(_SORT_OPTIONS)
        self._refresh()

    def action_cursor_right(self) -> None:
        self._cursor = (self._cursor + 1) % len(_SORT_OPTIONS)
        self._refresh()

    def action_apply(self) -> None:
        """Header-click semantic: same axis → toggle direction; different axis → switch to that axis
        ascending. Dialog stays open so the user can keep tweaking."""
        chosen = _SORT_OPTIONS[self._cursor]
        if chosen == self._vm.sort_by:
            new_dir: Literal["asc", "desc"] = (
                "desc" if self._vm.sort_dir == "asc" else "asc"
            )
        else:
            new_dir = "asc"
        self._vm.set_sort(chosen, new_dir)

    def action_reset(self) -> None:
        """Restore the default sort (``id`` ascending). Lands the cursor on ``id`` regardless of
        whether the sort actually changes — the dialog stays open."""
        self._cursor = 0
        self._vm.set_sort("id", "asc")

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._tab.toggle_dialog(name)  # type: ignore[arg-type]
