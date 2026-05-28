"""SortMenu — generic sort-axis picker over any ``SortableVMMixin`` VM.

Layout: one horizontal row of axes (in `vm.sort_options()` order) plus a keybinding hint row.
The active axis is decorated with an arrow + brackets; the cursor option renders bold (gold
on focus, grey off-focus). Labels stay fixed-width — no `►` prefix that would shift columns.

Keys
----
  * ``left`` / ``right`` — move cursor (wrap)
  * ``enter`` — apply: toggle direction on the active axis, otherwise switch to the cursor
    axis ascending. Dialog stays open.
  * ``r`` — reset to ``sort_options()[0]`` ascending.
  * ``escape`` — dismiss via the constructor-supplied ``on_close`` callback.

Sibling-dialog swap keys (``d`` / ``s`` / ``f`` / ``e``) are intentionally unbound so they
bubble to the parent's BINDINGS, which owns the dialog mutex.

Subclasses may override ``_extra_hint() -> Text | None`` to append a state-driven warning to
the hint row (used by the entries tab to flag "Applying clears your selection." during
multi-select).
"""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from .view_model_mixin import SortableVMMixin, SortDirection

VM = TypeVar("VM", bound=SortableVMMixin)


class SortMenu(Static, Generic[VM], can_focus=True):
    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "apply", show=False),
        Binding("r", "reset", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: VM,
        on_close: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._on_close = on_close
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
        """Land the cursor on the active axis so toggling direction is one ``enter`` away.
        Falls back to index 0 if the active axis isn't currently surfaced."""
        options = self._vm.sort_options()
        try:
            self._cursor = options.index(self._vm.sort_by)
        except ValueError:
            self._cursor = 0

    def _extra_hint(self) -> Text | None:
        """Optional inline hint appended to the keybinding row. Default ``None``."""
        return None

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        options = self._vm.sort_options()
        active_idx = (
            options.index(self._vm.sort_by) if self._vm.sort_by in options else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Active axis renders in default fg so the arrow + brackets carry the "live sort"
        # signal; cursor highlight is layered on top via a brighter style.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(options):
            is_active = i == active_idx
            is_cursor = i == self._cursor
            label = f"{arrow}[{option}]" if is_active else option
            if is_cursor:
                style = cursor_color
            elif is_active:
                style = ""
            else:
                style = "#787878"
            text.append(label, style=style)
            if i < len(options) - 1:
                text.append("   ")
        text.append("\n")

        text.append(
            "← / → move • enter apply • r reset • s/esc dismiss", style="dim",
        )
        extra = self._extra_hint()
        if extra is not None:
            text.append("   ")
            text.append(extra)
        return text

    def action_cursor_left(self) -> None:
        options = self._vm.sort_options()
        if not options:
            return
        self._cursor = (self._cursor - 1) % len(options)
        self._refresh()

    def action_cursor_right(self) -> None:
        options = self._vm.sort_options()
        if not options:
            return
        self._cursor = (self._cursor + 1) % len(options)
        self._refresh()

    def action_apply(self) -> None:
        options = self._vm.sort_options()
        if not options:
            return
        chosen = options[self._cursor]
        if chosen == self._vm.sort_by:
            new_dir: SortDirection = "desc" if self._vm.sort_dir == "asc" else "asc"
        else:
            new_dir = "asc"
        self._vm.set_sort(chosen, new_dir)

    def action_reset(self) -> None:
        options = self._vm.sort_options()
        if not options:
            return
        self._cursor = 0
        self._vm.set_sort(options[0], "asc")

    def action_cancel(self) -> None:
        self._on_close()
