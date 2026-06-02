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

from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.app.chat_pane.interrupts.base import InterruptVMBase
from rhizome.app.chat_pane.interrupts.sql import SqlConfirmationVM


_MAX_CELL_WIDTH = 40
_MAX_SNIPPET_WIDTH = 60


def _truncate_cell(value: Any, max_len: int = _MAX_CELL_WIDTH) -> str:
    """Stringify ``value``, collapse newlines, ellipsize to ``max_len``."""
    s = str(value).replace("\n", " ").replace("\r", "")
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


class SqlConfirmation(NavigableFeedItemViewBase[SqlConfirmationVM]):
    """Renders ``SqlConfirmationVM``. Up/Down move the cursor; Enter confirms."""

    DEFAULT_CSS = """
    SqlConfirmation {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 0 2;
    }
    SqlConfirmation.--resolved {
        color: $text-muted;
    }
    SqlConfirmation #sql-header {
        color: rgb(220,160,50);
        margin-bottom: 1;
    }
    SqlConfirmation #sql-statement {
        background: rgb(40,40,50);
        padding: 1 2;
        margin-bottom: 1;
    }
    SqlConfirmation #sql-no-preview {
        color: rgb(150,150,150);
        margin-bottom: 1;
    }
    SqlConfirmation DataTable {
        height: auto;
        max-height: 14;
        margin-bottom: 1;
    }
    SqlConfirmation #sql-truncation-note {
        color: rgb(150,150,150);
        margin-bottom: 1;
    }
    SqlConfirmation #sql-options {
        height: auto;
    }
    SqlConfirmation #sql-hint {
        color: rgb(100,100,100);
    }
    SqlConfirmation #sql-collapsed {
        height: auto;
        display: none;
    }
    SqlConfirmation.--resolved #sql-header,
    SqlConfirmation.--resolved #sql-statement,
    SqlConfirmation.--resolved #sql-no-preview,
    SqlConfirmation.--resolved #sql-preview-table,
    SqlConfirmation.--resolved #sql-truncation-note,
    SqlConfirmation.--resolved #sql-options,
    SqlConfirmation.--resolved #sql-hint {
        display: none;
    }
    SqlConfirmation.--resolved #sql-collapsed {
        display: block;
    }
    """

    BINDINGS = [
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
        ("enter", "confirm", "Confirm"),
        ("ctrl+c", "cancel", "Cancel"),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("⚠  " + self._header_text(), id="sql-header")
        yield Static(self._vm.sql, id="sql-statement")

        if self._vm.has_preview:
            yield DataTable(show_cursor=False, zebra_stripes=True, id="sql-preview-table")
            note = self._truncation_note_text()
            if note is not None:
                yield Static(note, id="sql-truncation-note")
        else:
            yield Static("(no preview available)", id="sql-no-preview")

        yield Static("", id="sql-options")
        yield Static("  (ctrl+c to cancel)", id="sql-hint")
        yield Static("", id="sql-collapsed")

    def on_mount(self) -> None:
        # Populate the table once — its contents are derived from immutable VM state.
        if self._vm.has_preview:
            table = self.query_one("#sql-preview-table", DataTable)
            for col in self._vm.preview_columns:
                table.add_column(str(col))
            for row in self._vm.preview_rows:
                table.add_row(*[_truncate_cell(v) for v in row])

        self.focus()
        self._refresh()

    # --- rendering helpers ----------------------------------------------------------------------

    def _header_text(self) -> str:
        rc = self._vm.row_count
        if rc is None:
            return "SQL Modification"
        return f"SQL Modification — {rc} row(s) affected"

    def _truncation_note_text(self) -> str | None:
        omitted = self._vm.truncated_row_count
        if omitted is None:
            return None
        return f"  … {omitted} more row(s) not shown"

    def _sql_oneline(self) -> str:
        snippet = " ".join(self._vm.sql.split())
        if len(snippet) > _MAX_SNIPPET_WIDTH:
            snippet = snippet[: _MAX_SNIPPET_WIDTH - 1] + "…"
        return snippet

    def _refresh(self) -> None:
        # Options block — repaint based on current cursor + resolved state.
        options_text = Text()
        for i, opt in enumerate(self._vm.options):
            if i > 0:
                options_text.append("\n")
            label = f"  {i + 1}. {opt}"
            if self._vm.resolved:
                options_text.append(label, style="rgb(100,100,100)")
            elif i == self._vm.cursor:
                options_text.append(label, style="bold white")
            else:
                options_text.append(label, style="rgb(100,100,100)")
        self.query_one("#sql-options", Static).update(options_text)

        # Resolved-state collapse: CSS toggles visibility off the --resolved class.
        if self._vm.resolved:
            self.add_class("--resolved")

            if self._vm.cancelled:
                tail_style = "rgb(200,100,100)"
                tail_label = "cancelled"
            else:
                approved = self._vm.result == "Approve"
                tail_style = "rgb(100,180,100)" if approved else "rgb(200,100,100)"
                tail_label = "approved" if approved else "denied"

            collapsed = Text.assemble(
                ("  ", ""),
                (self._sql_oneline(), "rgb(140,140,150)"),
                ("  — ", "rgb(100,100,100)"),
                (tail_label, tail_style),
            )
            self.query_one("#sql-collapsed", Static).update(collapsed)
        else:
            self.remove_class("--resolved")

    # --- bindings -------------------------------------------------------------------------------

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()

    def action_cancel(self) -> None:
        self._vm.cancel()
