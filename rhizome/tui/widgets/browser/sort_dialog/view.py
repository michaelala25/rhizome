"""SortDialog — generic sort-axis picker over any ``SortableViewModelMixin`` VM.

Renders horizontally, mirroring the data table's column order: each entry from the VM's
``sort_options()`` laid out left-to-right. The active sort is decorated with an arrow
(``↑`` / ``↓``) and brackets; the cursor option is shown in a bold accent colour (no ``►``
prefix — keeping the row at a fixed width avoids labels jumping around as the cursor moves).
A second line carries the keybinding hint, optionally extended with a subclass-supplied
inline supplemental hint via the ``_extra_hint`` hook.

Keys
----
  * ``left`` / ``right`` move the cursor (with wrap)
  * ``enter`` applies (toggles direction when on the active axis, otherwise switches to that
    axis ascending). Dialog stays open so the user can keep tweaking.
  * ``r`` resets to the first option ascending. The VM controls the option order, so the
    first option doubles as the canonical "default sort".
  * ``escape`` dismisses without applying. Routes to the constructor-provided ``on_close``
    callback so the dialog stays decoupled from any specific container API — sibling-dialog
    swap keys (``d`` / ``f`` / ``e`` / ``s``) are deliberately *not* bound here so they
    bubble to the parent's bindings; the parent owns the dialog mutex and decides what its
    siblings are.

Extending
---------
Subclasses can override ``_extra_hint() -> Text | None`` to supply a state-driven inline
warning appended to the keybinding hint row. The base returns ``None``; the entries-tab uses
this to surface a "Applying clears your selection." warning while multi-select is on.
"""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from .view_model_mixin import SortableViewModelMixin, SortDirection

VM = TypeVar("VM", bound=SortableViewModelMixin)


class SortDialog(Static, Generic[VM], can_focus=True):
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
        # Called from ``escape``. Decouples the dialog from the parent's exact dismissal API
        # (a tab might call ``hide_dialog``; a screen might pop itself; either way the dialog
        # just hands control back).
        self._on_close = on_close
        # Cursor index into ``vm.sort_options()``. Landed on the currently-active sort axis at
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
        """Land the cursor on the currently-active sort axis so the most common action
        (toggle direction of the active sort) is one ``enter`` away. Falls back to index 0 if
        the active axis isn't surfaced (e.g. the VM trimmed an axis the user previously had
        selected)."""
        options = self._vm.sort_options()
        try:
            self._cursor = options.index(self._vm.sort_by)
        except ValueError:
            self._cursor = 0

    def _extra_hint(self) -> Text | None:
        """Optional supplemental hint appended inline to the keybinding hint row. Default
        returns ``None``; subclasses override to surface state-driven warnings (e.g. "Applying
        clears your selection." while multi-select is on)."""
        return None

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        options = self._vm.sort_options()
        active_idx = (
            options.index(self._vm.sort_by) if self._vm.sort_by in options else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Cursor colour: bright gold on focus, dim grey otherwise. The active axis itself
        # always renders in the default fg so the arrow + brackets carry the "this is the
        # live sort" signal.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(options):
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
        """Header-click semantic: same axis → toggle direction; different axis → switch to
        that axis ascending. Dialog stays open so the user can keep tweaking."""
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
        """Restore the default sort (first surfaced option, ascending). Lands the cursor on
        that option regardless of whether the sort actually changes — the dialog stays open."""
        options = self._vm.sort_options()
        if not options:
            return
        self._cursor = 0
        self._vm.set_sort(options[0], "asc")

    def action_cancel(self) -> None:
        self._on_close()
