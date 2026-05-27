"""MultiSelectableDataTable — DataTable subclass that surfaces multi-select keybindings.

Keys (all no-ops outside multi-select; the VM guards):

  * ``space`` — toggle the cursor row's membership in the selection.
  * ``shift+down`` / ``shift+up`` — idempotent add + cursor step. Held-key repeat sweeps a
    contiguous block.

The widget is purely a binding shim — it owns no state beyond the VM reference. Visual
styling (marker column, highlight, zebra wash) and pagination (auto-load-more on cursor-
down) are deliberately left to subclasses / parent views.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from textual.binding import Binding
from textual.widgets import DataTable

from .view_model_mixin import MultiSelectableViewModelMixin

VM = TypeVar("VM", bound=MultiSelectableViewModelMixin)


class MultiSelectableDataTable(DataTable, Generic[VM]):
    BINDINGS = [
        Binding("space", "toggle_selection", show=False),
        # Bound as two separate keys (not a "shift+up,shift+down" combo) because each
        # direction needs its own cursor step after the idempotent add.
        Binding("shift+down", "select_down", show=False),
        Binding("shift+up", "select_up", show=False),
    ]

    def __init__(self, view_model: VM, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm: VM = view_model

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    async def action_select_down(self) -> None:
        # Step via the inherited async ``action_cursor_down`` so subclass overrides
        # (e.g. auto-load-more at the bottom edge) still run.
        self._vm.add_current_to_selection()
        await self.action_cursor_down()

    def action_select_up(self) -> None:
        self._vm.add_current_to_selection()
        self.action_cursor_up()
