"""``SharedTopicSetter`` — focusable strip at the top of the flashcard-proposal widget.

Single-line summary: ``Set topic for all flashcards - <topic>``. Blurred, the whole line renders
in a flat grey. Focused, the line switches to white and a ``► `` cursor arrow appears on the
left, mirroring the cursor style used by the global choices list.

``enter`` posts ``SetTopicRequested(scope="all")``; the parent ``FlashcardProposal`` owns the
modal and applies the result via ``vm.set_topic_all``. Plain ``up`` / ``down`` aren't bound here
so they bubble to the parent's arrow bindings without any local plumbing.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalVM
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.flashcard_proposal.messages import SetTopicRequested


class SharedTopicSetter(Static, can_focus=True):

    DEFAULT_CSS = """
    SharedTopicSetter {
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    """

    BINDINGS = [
        Keybind.MenuConfirm.as_binding("set_topic_all", show=False),
    ]

    def __init__(self, vm: FlashcardProposalVM, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vm = vm

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Both the cursor arrow and the text color flip on focus; re-render so the focused styling
        # applies. ``call_after_refresh`` so ``has_focus`` reads True by the time we repaint.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def action_set_topic_all(self) -> None:
        # Defensive: the parent FlashcardProposal's ``on_set_topic_requested`` already gates on
        # EDITING, but stop the message at the source so we don't fire spurious traffic during
        # DONE (when this widget is non-focusable anyway, but a mouse click could still hit the
        # binding through some future surface).
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self.post_message(SetTopicRequested(scope="all"))

    def _refresh(self) -> None:
        focused = self.has_focus
        text_color = "white" if focused else "#6a6a6a"

        text = Text()
        if focused:
            text.append("► ", style="bold #ffd700")
        else:
            text.append("  ")
        # Keybinding prefix; same grey as the hint strip below the flashcard list.
        text.append("shift+t", style="#a0a0a0")
        text.append("  ")
        text.append("Set topic for all flashcards - current: ", style=text_color)

        flashcards = self._vm.flashcards
        if not flashcards:
            text.append("(no flashcards)", style=text_color)
        else:
            ids = {f.topic_id for f in flashcards}
            if len(ids) > 1:
                text.append("(mixed)", style=text_color)
            else:
                only = next(iter(ids))
                if only is None:
                    text.append("(none)", style=text_color)
                else:
                    name = flashcards[0].topic_name or f"#{only}"
                    text.append(name, style=text_color)
        self.update(text)
