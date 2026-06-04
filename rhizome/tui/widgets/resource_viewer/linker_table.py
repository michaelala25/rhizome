"""``ResourceLinkerTable`` — staged link/unlink table over ``ResourceLinkerVM``.

A ``DataTable`` showing the topic's link picture as ``[*linked, <boundary>, *pool]``: the pinned
linked section (frozen at fetch), a divider row, then the searchable/paginated pool of every other
resource. Columns: sel / name / tokens. ``space`` flips the cursor row's staged membership
(``[x]``/``[ ]``); the pool auto-paginates at the bottom edge via ``vm.load_more``.

This widget only *stages* — committing / reverting lives on the Accept/Cancel menu
(``ResourceLinkerAccept``). The highlighted resource is mirrored into the VM (``set_cursor``) so the
preview can feed off it.

Three-path refresh keyed off a row signature (linked ids · boundary · pool ids): an equal signature
is a staging toggle → in-place cell rewrites; a prefix-extended signature is a ``load_more`` → append;
anything else (topic / search / commit) → full rebuild.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rhizome.app.resource_viewer.linker import ResourceLinkerVM
from rhizome.db import Resource
from rhizome.tui.keybindings import Keybind

# Boundary sentinel for the row-signature tuple. Negative is safe — resource ids are positive
# autoincrement ints, so it can't collide with a real row's id.
_BOUNDARY_SIG: int = -1
_BOUNDARY_ROW_KEY = "__boundary__"

_NAME_COLUMN_WIDTH = 28

# Row palettes: bright green for staged-linked rows (mirrors the loader tree / flashcards relink),
# dim zebra otherwise, and a near-invisible grey for the pinned/pool divider.
_STAGED_STYLE = "bold #5fd75f"
_SEP_STYLE = "#3a3a3a"


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class ResourceLinkerTable(DataTable):
    """Staged link/unlink table over ``ResourceLinkerVM``. See module docstring."""

    BINDINGS = [
        Keybind.Toggle.             as_binding("toggle_link",  show=False),
        Keybind.ResourceFocusSearch.as_binding("focus_search", show=False),
        # Commit / discard the staged diff without travelling to the Accept/Cancel menu — mirrors a
        # ConfirmableTextArea (ctrl+enter accepts, esc discards). ``ctrl+j`` is the legacy-terminal
        # alias for ctrl+enter. Both confirm/discard bubble when nothing is staged.
        Keybind.ResourceConfirmEdits.as_binding("confirm_edits", show=False),
        Keybind.CloseMenu.           as_binding("discard_edits", show=False),
    ]

    DEFAULT_CSS = """
    ResourceLinkerTable {
        background: transparent;
    }
    ResourceLinkerTable:focus {
        background-tint: transparent;
    }
    """

    def __init__(self, view_model: ResourceLinkerVM, **kwargs: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._vm = view_model
        # Row-signature edge detector driving the three-path refresh (rebuild / extend / inplace),
        # so cursor + scroll survive a staging toggle and a load_more append.
        self._last_row_signature: tuple[int, ...] | None = None

    # ------------------------------------------------------------------
    # Mount lifecycle + subscription wiring
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.add_column("sel", width=3)
        self.add_column("name", width=_NAME_COLUMN_WIDTH)
        self.add_column("tokens")
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        linked = self._vm.linked_resources
        remaining = self._vm.remaining_resources
        new_sig: tuple[int, ...] = (
            tuple(r.id for r in linked) + (_BOUNDARY_SIG,) + tuple(r.id for r in remaining)
        )

        old_sig = self._last_row_signature
        if new_sig == old_sig:
            path, start = "inplace", 0
        elif (
            old_sig is not None
            and len(new_sig) > len(old_sig)
            and new_sig[: len(old_sig)] == old_sig
        ):
            path, start = "extend", len(old_sig)
        else:
            path, start = "rebuild", 0
            self.clear()

        n_linked = len(linked)
        for i in range(start, len(new_sig)):
            if i == n_linked:  # the pinned/pool divider
                cells = self._boundary_cells()
                row_key = _BOUNDARY_ROW_KEY
            else:
                resource = linked[i] if i < n_linked else remaining[i - n_linked - 1]
                cells = self._resource_cells(resource, zebra_index=i)
                row_key = str(new_sig[i])
            if path == "inplace":
                for col, value in enumerate(cells):
                    self.update_cell_at(Coordinate(i, col), value)
            else:
                self.add_row(*cells, key=row_key)

        self._last_row_signature = new_sig

        # Restore the cursor after a rebuild (``clear()`` resets it to row 0). ``move_cursor`` fires
        # ``RowHighlighted`` → ``vm.set_cursor``; the VM's equality guard breaks the loop.
        if path == "rebuild" and len(new_sig) > 0 and 0 <= self._vm.cursor < len(new_sig):
            self.move_cursor(row=self._vm.cursor, animate=False)

    def _resource_cells(self, resource: Resource, *, zebra_index: int) -> tuple[Text, Text, Text]:
        staged = self._vm.is_staged_linked(resource.id)
        if staged:
            style = _STAGED_STYLE
        else:
            style = "#a0a0a0" if zebra_index % 2 else ""
        marker = "[x]" if staged else "[ ]"
        return (
            Text(marker, style=style),
            Text(resource.name, style=style),
            Text(_fmt_tokens(resource.estimated_tokens), style=style),
        )

    def _boundary_cells(self) -> tuple[Text, Text, Text]:
        return (
            Text("───", style=_SEP_STYLE),
            Text("─" * _NAME_COLUMN_WIDTH, style=_SEP_STYLE),
            Text("─────", style=_SEP_STYLE),
        )

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge. ``vm.load_more`` is a no-op mid-fetch or
        when nothing more is available, so calling at the edge is safe."""
        if (
            self._vm.remaining_has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_toggle_link(self) -> None:
        self._vm.toggle_current()

    def action_focus_search(self) -> None:
        if self.screen is not None:
            try:
                self.screen.query_one("#rv-linker-search").focus()
            except Exception:
                pass

    async def action_confirm_edits(self) -> None:
        await self._vm.accept()

    def action_discard_edits(self) -> None:
        self._vm.cancel()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # ctrl+enter / esc only act on a pending diff; otherwise return None so the key bubbles (esc
        # keeps its usual "back out" meaning, ctrl+enter falls through to any outer handler).
        if action in ("confirm_edits", "discard_edits"):
            return True if self._vm.is_dirty_staging else None
        return True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Mirror the highlighted row into the VM (feeds the preview). Equality-guarded VM-side, so
        # the programmatic ``move_cursor`` in ``_refresh`` doesn't loop.
        self._vm.set_cursor(event.cursor_row)
