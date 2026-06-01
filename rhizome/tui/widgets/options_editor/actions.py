from typing import ClassVar

from rich.text import Text
from textual.message import Message

from rhizome.app.options_editor import OptionsEditorVM
from rhizome.tui.widgets.shared.choices_list import ChoiceList


class OptionsEditorActions(ChoiceList[OptionsEditorVM]):
    DEFAULT_CSS = """
    OptionsEditorActions {
        height: auto;
        padding: 0 1 0 3;
        background: transparent;
    }
    """

    ORIENTATION = "vertical"
    LEAD = None
    HINT = None

    class Dismissed(Message):
        """Footer-local request to dismiss the editor. The parent ``OptionsEditor`` catches
        this and re-emits its own external ``OptionsEditorDismissed`` for the chat pane."""

    CHOICES: ClassVar[dict[str, str]] = {
        "Apply": "_apply",
        "Reset": "_reset",
        "Dismiss": "_dismiss",
    }

    _ACTION_INFO: ClassVar[dict[str, tuple[str, str]]] = {
        "Apply":   ("ctrl+a", "apply staged edits"),
        "Reset":   ("ctrl+r", "discard staged edits"),
        "Dismiss": ("ctrl+c", "close the editor"),
    }

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Boundary fall-through
        n = len(self.choices())
        if action == "cursor_up":
            return self._cursor > 0
        if action == "cursor_down":
            return self._cursor < n - 1
        return True

    # ------------------------------------------------------------------
    # Choice actions
    # ------------------------------------------------------------------

    async def _apply(self) -> None:
        if self._vm:
            await self._vm.apply()

    def _reset(self) -> None:
        if self._vm:
            self._vm.reset()

    def _dismiss(self) -> None:
        self.post_message(self.Dismissed())

    def action_cancel(self) -> None:
        # ``escape`` on the menu also dismisses, matching the legacy editor's ``x`` button.
        self.post_message(self.Dismissed())

    def _render_choice(self, label: str, selected: bool) -> Text:
        keybinding, description = self._ACTION_INFO[label]
        focused = self.has_focus
        text = Text()
        if selected:
            text.append("► ", style="bold #ffd700" if focused else "#707070")
        else:
            text.append("  ")
        text.append(keybinding, style="#a0a0a0")
        text.append("  ")
        if selected and focused:
            label_style = "bold white"
            description_style = "#909090"
        else:
            label_style = "#a0a0a0"
            description_style = "#707070"
        text.append(f"{label:<8}", style=label_style)
        text.append(f"  - {description}", style=description_style)
        return text