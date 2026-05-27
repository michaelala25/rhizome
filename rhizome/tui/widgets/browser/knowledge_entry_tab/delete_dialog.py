"""Delete confirmation dialog. Targets the multi-select selection or the cursor entry depending on
mode (the VM's ``delete_selected_entries`` resolves this internally)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from .view_model import KnowledgeEntryBrowserTabViewModel

if TYPE_CHECKING:
    from .view import KnowledgeEntryBrowserTabView


class _DeleteConfirm(Static, can_focus=True):
    """Renders three lines: a header explaining the action (entry count + the no-flashcards-harmed
    promise), then two indented choice rows (Confirm / Cancel). Cursor brightness tracks focus."""

    BINDINGS = [
        Binding("up", "choice_up", show=False),
        Binding("down", "choice_down", show=False),
        Binding("enter", "choice_confirm", show=False),
        Binding("escape", "cancel", show=False),
        # Mutex siblings: pressing one of these from inside the delete dialog swaps to the other.
        Binding("s", "swap_to('sort')", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("e", "swap_to('edit')", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._tab = tab
        # 0 = Confirm, 1 = Cancel. Reset to 0 every time the dialog is shown (``prepare_for_show``).
        self._choice_cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Called by the tab right before this dialog becomes visible. Reset the choice cursor to
        ``Confirm`` so each open starts fresh."""
        self._choice_cursor = 0

    def _refresh(self) -> None:
        # Note: not ``_render`` — that's a Textual-internal name (the widget's own ``_render``
        # returns the cached Visual). Naming this method ``_render`` shadows the framework hook and
        # Textual tries to use the returned ``rich.text.Text`` as a ``Visual``, blowing up in
        # ``to_strips``.
        self.update(self._render_dialog())

    def _render_dialog(self) -> Text:
        count = self._tab.selection_target_count()
        noun = "entry" if count == 1 else "entries"
        # In single-select mode the lead-in is just "Delete this entry?" — "selected" reads weird
        # when there's no visible selection mark. Multi-select keeps the existing phrasing.
        scope_word = "selected " if self._vm.multi_select_active else ""
        cursor_style = "bold" if self.has_focus else "#6a6a6a"
        text = Text()
        text.append(f"Delete {count} {scope_word}{noun}? ", style="bold")
        text.append("Linked flashcards will not be affected.", style="dim")
        text.append("\n")
        labels = ("Confirm", "Cancel")
        for i, label in enumerate(labels):
            chosen = i == self._choice_cursor
            if chosen:
                text.append("► ", style=cursor_style)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
            if i < len(labels) - 1:
                text.append("\n")
        return text

    def action_choice_up(self) -> None:
        self._choice_cursor = (self._choice_cursor - 1) % 2
        self._refresh()

    def action_choice_down(self) -> None:
        self._choice_cursor = (self._choice_cursor + 1) % 2
        self._refresh()

    async def action_choice_confirm(self) -> None:
        if self._choice_cursor == 0:
            await self._vm.delete_selected_entries()
        self._tab.hide_dialog()

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._tab.toggle_dialog(name)  # type: ignore[arg-type]
