from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel
from rhizome.tui.keybindings import Keybind

from .messages import SetTopicRequested


class EntryList(DataTable):
    """Four-column DataTable backed by ``vm.entries``. Cursor lives in the table; we only forward
    keypresses to the VM. Subscribes to ``OnEntriesChanged`` for per-row repaint."""

    can_focus = True

    DEFAULT_CSS = """
    EntryList {
        width: 1fr;
        height: auto;
        min-height: 5;
        max-height: 20;
    }
    """

    BINDINGS = [
        Keybind.ProposalToggleExclude.as_binding("toggle_exclude", show=False),
        Keybind.ProposalCycleType.    as_binding("cycle_type",     show=False),
        Keybind.ProposalSetTopic.     as_binding("set_topic",      show=False),
    ]

    _CONTENT_MIN_WIDTH = 15

    def __init__(self, vm: CommitProposalModel, **kwargs: Any) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            zebra_stripes=True,
            cursor_type="row",
            **kwargs,
        )
        self.model = vm
        self._content_key = None

    def on_mount(self) -> None:
        self.add_columns("Title", "Type", "Topic")
        self._content_key = self.add_column("Content", width=self._CONTENT_MIN_WIDTH)

        for _ in self.model.entries:
            self.add_row("", "", "", "")

        self.model.subscribe(self.model.Callbacks.OnEntriesChanged, self._on_entries_changed)

        for i in range(len(self.model.entries)):
            self._refresh_row(i)

    def on_unmount(self) -> None:
        self.model.unsubscribe(self.model.Callbacks.OnEntriesChanged, self._on_entries_changed)

    def on_resize(self) -> None:
        self._fit_content_column()

    def _fit_content_column(self) -> None:
        if self._content_key is None or self.size.width <= 0:
            return

        content_col = self.columns.get(self._content_key)
        if content_col is None:
            return

        others = sum(
            c.get_render_width(self) for k, c in self.columns.items() if k != self._content_key
        )
        target = max(self._CONTENT_MIN_WIDTH, self.size.width - others - 2 * self.cell_padding)

        if content_col.width != target:
            content_col.width = target
            self.refresh(layout=True)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "cursor_up":
            return self.cursor_row > 0
        if action == "cursor_down":
            return self.cursor_row < self.row_count - 1
        if action == "select_cursor":
            return False  # bubble enter to the parent's collapse toggle
        return True

    def action_toggle_exclude(self) -> None:
        if self.model.is_done:
            return
        self.model.toggle_excluded(self.cursor_row)

    def action_cycle_type(self) -> None:
        if self.model.is_done:
            return
        self.model.cycle_entry_type(self.cursor_row)

    def action_set_topic(self) -> None:
        if self.model.is_done:
            return
        self.post_message(SetTopicRequested(scope="current"))

    def _on_entries_changed(self, indices: list[int]) -> None:
        for idx in indices:
            if 0 <= idx < self.row_count:
                self._refresh_row(idx)

    def _refresh_row(self, idx: int) -> None:
        entry = self.model.entries[idx]
        excluded = self.model.is_excluded(idx)
        style = "dim strike" if excluded else ""

        title = Text(entry.title or "(untitled)", style=style)
        type_ = Text(entry.entry_type.value, style=style or "dim")
        topic = Text(entry.topic.name if entry.topic else "(none)", style=style or "dim")

        preview = " ".join((entry.content or "").split()) or "(empty)"
        content = Text(preview, style=style or "dim")

        self.update_cell_at(Coordinate(idx, 0), title)
        self.update_cell_at(Coordinate(idx, 1), type_)
        self.update_cell_at(Coordinate(idx, 2), topic)
        self.update_cell_at(Coordinate(idx, 3), content)
