"""Read-only preview of the cursor entry's ``content``. Visible only in ``LINKED_FLASHCARDS``
(the editable details panel covers the same job in ``ENTRIES``). Non-focusable — keyboard never
lands here; mouse-wheel scroll still works.
"""

from __future__ import annotations

from typing import Any

from textual.widgets import TextArea

from rhizome.app.browser.tabs.entries.tab import EntryTabModel


class EntryPreview(TextArea):
    """Subscribes to both the tab VM's ``dirty`` (refetches, post-save repaints) and the details
    VM's ``dirty`` (cursor moves — ``set_cursor`` fires only the details dirty, see view_model.py
    for why). Re-reads ``entries[cursor]`` on each fire."""

    can_focus = False

    DEFAULT_CSS = """
    EntryPreview {
        background: transparent;
        border: solid #3a3a3a;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: EntryTabModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            read_only=True, show_line_numbers=False, soft_wrap=True, **kwargs,
        )
        self._vm = view_model
        self.border_title = "[dim]entry content[/]"
        self.border_title_align = "left"

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._vm.details.subscribe(self._vm.details.Callbacks.OnDirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._vm.details.unsubscribe(self._vm.details.Callbacks.OnDirty, self._refresh)

    def _refresh(self) -> None:
        entries = self._vm.entries
        cursor = self._vm.cursor
        if not entries or cursor >= len(entries):
            target = ""
        else:
            target = entries[cursor].content or ""
        if self.text != target:
            self.text = target
