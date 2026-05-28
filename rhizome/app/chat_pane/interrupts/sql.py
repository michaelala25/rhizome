"""SqlConfirmation interrupt for the chat-pane MVVM rewrite.

Asks the user to approve or deny a SQL modification, showing the statement plus a preview DataTable
of affected rows. Mirrors the legacy ``SqlConfirmation`` widget but split MVVM-style: the VM holds
the data + cursor + future, the view renders Textual widgets.

The DataTable's contents are derived from VM state that is fixed at construction time (preview rows
never change), so we populate it once on mount and never diff it during ``_refresh``. ``_refresh``
only repaints the options block and toggles the resolved-state collapse.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from rhizome.app.chat_pane.interrupts.base import InterruptVMBase


_MAX_CELL_WIDTH = 40
_MAX_SNIPPET_WIDTH = 60


def _truncate_cell(value: Any, max_len: int = _MAX_CELL_WIDTH) -> str:
    """Stringify ``value``, collapse newlines, ellipsize to ``max_len``."""
    s = str(value).replace("\n", " ").replace("\r", "")
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


class SqlConfirmationVM(InterruptVMBase):
    """Holds the SQL + preview data + Approve/Deny cursor for a SQL-modification interrupt."""

    def __init__(
        self,
        sql: str,
        preview_columns: list[str] | None = None,
        preview_rows: list[list[Any]] | None = None,
        row_count: int | None = None,
    ) -> None:
        super().__init__()
        self.is_navigable = True
        self.sql = sql
        self.preview_columns: list[str] = list(preview_columns or [])
        self.preview_rows: list[list[Any]] = list(preview_rows or [])
        self.row_count = row_count
        self._options: list[str] = ["Approve", "Deny"]
        self._cursor: int = 0

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> SqlConfirmationVM:
        preview = value.get("preview", {})
        return cls(
            sql=value.get("sql", ""),
            preview_columns=preview.get("columns", []),
            preview_rows=preview.get("rows", []),
            row_count=value.get("row_count"),
        )

    # --- read-only view surface -----------------------------------------------------------------

    @property
    def options(self) -> list[str]:
        return list(self._options)

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def has_preview(self) -> bool:
        return bool(self.preview_columns)

    @property
    def truncated_row_count(self) -> int | None:
        """How many affected rows are *not* shown in the preview, or ``None`` if unknown / none."""
        if self.row_count is None:
            return None
        omitted = self.row_count - len(self.preview_rows)
        return omitted if omitted > 0 else None

    # --- mutators -------------------------------------------------------------------------------

    def move_cursor(self, delta: int) -> None:
        if self.resolved:
            return
        n = len(self._options)
        if n == 0:
            return
        new = (self._cursor + delta) % n
        if new == self._cursor:
            return
        self._cursor = new
        self.emit(self.dirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        self.resolve(self._options[self._cursor])
