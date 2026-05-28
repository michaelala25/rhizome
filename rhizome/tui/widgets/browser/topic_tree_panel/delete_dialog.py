"""``TopicsDeleteMenu`` — inline confirm-delete dialog that occupies the panel's bottom slot
in place of ``TopicDetails`` while a delete is being confirmed.

A single ``ChoiceList`` subclass: the prompt ("Delete <name> and its entire subtree?") lives in
``_render_header``, the choices row sits below. VM-less — the panel feeds the target name via
``prepare_for_show`` and catches the nested ``Accepted`` / ``Cancelled`` messages through
Textual's message pump (``on_delete_dialog_view_<name>``) to drive the actual delete worker.
"""

from __future__ import annotations

from rich.text import Text

from textual.message import Message

from ..choices import ChoiceList


class TopicsDeleteMenu(ChoiceList[None]):
    """Cancel/Delete row with an inline ``Delete "<name>"?`` header. Cancel is first so the
    default cursor lands there — the user has to deliberately advance to ``Delete`` and press
    enter, which makes fat-fingering away an entire subtree much harder."""

    CHOICES = {"Cancel": "_cancel", "Delete": "_accept"}
    LEAD = "Confirm: "
    HINT = "← / → move • enter confirm • esc cancels"

    DEFAULT_CSS = """
    TopicsDeleteMenu {
        height: auto;
        padding: 1;
        border-top: solid #3a3a3a;
        display: none;
    }
    """

    # ------------------------------------------------------------------
    # Messages — caught by the panel via on_delete_dialog_view_<name>.
    # ------------------------------------------------------------------

    class Accepted(Message):
        """User confirmed the delete."""

    class Cancelled(Message):
        """User dismissed the dialog (esc or the Cancel choice)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(view_model=None, **kwargs)
        self._target_name: str | None = None

    def prepare_for_show(self, topic_name: str | None) -> None:
        """Update the header target and reset the choice cursor to Cancel."""
        self._target_name = topic_name
        super().prepare_for_show()

    def _render_header(self) -> Text:
        # Bright red on the prompt so the destructiveness of the action reads at a glance — the
        # topic name itself is bold-red to set it apart from the surrounding prose. The second
        # line spells out the cascade so the user knows entries / flashcards go too, not just the
        # topic rows.
        prompt_style = "rgb(255,80,80)"
        name_style = "bold rgb(255,80,80)"
        text = Text()
        text.append("Delete ", style=prompt_style)
        if self._target_name:
            text.append(f'"{self._target_name}"', style=name_style)
        else:
            text.append("topic", style=name_style)
        text.append(" and all descendant topics?\n\n", style=prompt_style)
        text.append(
            "All entries and flashcards under this subtree will also be deleted.",
            style=prompt_style,
        )
        return text

    def _render_choice(self, label: str, selected: bool) -> Text:
        # Paint the ``Delete`` choice red in every state (and bold-red when selected) so the
        # destructive option visually mirrors the prompt above. Cancel keeps the base style.
        if label != "Delete":
            return super()._render_choice(label, selected)
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        if selected:
            text.append("► ", style=cursor_color)
            text.append(label, style="bold rgb(255,80,80)")
        else:
            text.append("  ")
            text.append(label, style="rgb(200,80,80)")
        return text

    def _accept(self) -> None:
        self.post_message(self.Accepted())

    def _cancel(self) -> None:
        self.post_message(self.Cancelled())

    def action_cancel(self) -> None:
        # ChoiceList's escape binding routes here.
        self.post_message(self.Cancelled())
