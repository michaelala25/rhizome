"""Read-only scrollable preview of the cursor entry's ``content`` field.

Non-navigable (``can_focus=False``) so the keyboard never lands here; mouse-wheel scroll still
works. Only rendered when the parent tab is in ``State.LINKED_FLASHCARDS`` (CSS-driven via the
``-state-*`` class on the tab); in ``ENTRIES`` the details panel covers the same job.
"""

from __future__ import annotations

from typing import Any

from textual.widgets import TextArea

from .view_model import KnowledgeEntryBrowserTabViewModel


class _EntryContentPreview(TextArea):
    """Subscribes to the tab VM's ``dirty`` (refetches, post-save repaints) and the details VM's
    ``dirty`` (cursor moves — ``set_cursor`` routes through ``details.set_entry`` which fires the
    details dirty, but does **not** fire the tab dirty itself). Re-reads ``entries[cursor]`` on
    each fire and rebuilds the text."""

    can_focus = False

    DEFAULT_CSS = """
    _EntryContentPreview {
        background: transparent;
        border: solid #3a3a3a;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            read_only=True, show_line_numbers=False, soft_wrap=True, **kwargs,
        )
        self._vm = view_model
        self.border_title = "[dim]entry content[/]"
        self.border_title_align = "left"

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.details.subscribe(self._vm.details.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.details.unsubscribe(self._vm.details.dirty, self._refresh)

    def _refresh(self) -> None:
        entries = self._vm.entries
        cursor = self._vm.cursor
        if not entries or cursor >= len(entries):
            target = ""
        else:
            target = entries[cursor].content or ""
        if self.text != target:
            self.text = target
