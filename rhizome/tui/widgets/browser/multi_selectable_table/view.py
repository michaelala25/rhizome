"""MultiSelectableDataTable — DataTable subclass that surfaces the multi-select keybindings.

  * ``space`` — toggle the cursor row's membership in the selection.
  * ``shift+down`` / ``shift+up`` — idempotent add + cursor step (range-select sugar). Held-
    key terminal repeat sweeps a contiguous block.

All three are no-ops outside multi-select (the VM guards). The widget is purely a binding
shim — it owns no state of its own beyond the VM reference.

Auto-load-more on cursor-down (used by paginated tabs) is **not** included here. It's an
orthogonal concern (some tables want pagination but not multi-select, and vice-versa);
concrete subclasses or a separate mixin can compose it in.

Visual styling stays per-tab. The widget doesn't paint the ``[x]``/``[ ]`` marker column,
the bright-green selected row colour, or the ``-multi-select`` zebra wash — those are
rendering decisions that vary by domain and live inside the parent view's ``_refresh``.
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
        # ``shift+up`` / ``shift+down`` are range-select sugar: add the cursor row (idempotent)
        # and step in one keystroke. Held-key terminal repeat makes "hold shift, hold down"
        # sweep a contiguous block. Bound as two separate keys rather than a
        # ``"shift+up,shift+down"`` combo because each direction needs its own cursor step.
        Binding("shift+down", "select_down", show=False),
        Binding("shift+up", "select_up", show=False),
    ]

    def __init__(self, view_model: VM, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm: VM = view_model

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    async def action_select_down(self) -> None:
        """Add the cursor row to the selection, then step the cursor down via the inherited
        async ``action_cursor_down`` so any subclass-level overrides (e.g. auto-load-more
        at the bottom edge) get to run."""
        self._vm.add_current_to_selection()
        await self.action_cursor_down()

    def action_select_up(self) -> None:
        self._vm.add_current_to_selection()
        self.action_cursor_up()
