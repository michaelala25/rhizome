"""``EntryDetails`` — title/content editing pane on the right of the middle row.

Mirrors the structure of the browser's entry-details panel: title ``ConfirmableTextArea`` over
content ``ConfirmableTextArea`` over an Accept/Cancel ``ChoiceList`` that is only visible while
``EntryDetailsVM.is_dirty``.

Boundary navigation: plain ``up`` from the title and plain ``down`` from the bottom-most visible
field bubble ``BoundaryHit`` to the parent. Plain ``left`` from any field bubbles ``"left"`` so the
parent can return focus to the entry list. ``escape`` on either text area or the choices widget
fires ``vm.cancel()`` (silent discard).
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import TextArea

from rhizome.app.commit_proposal.entry_details import EntryDetailsVM
from rhizome.tui.widgets.browser.shared.choices_list import ChoiceList
from rhizome.tui.widgets.browser.shared.confirmable_text_area import ConfirmableTextArea


class _EntryDetailChoices(ChoiceList[EntryDetailsVM]):
    """Accept/Cancel for the focused entry's title/content edit."""

    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Edit: "
    HINT = "ctrl+enter to accept · esc to reset"

    def _accept(self) -> None:
        self._vm.accept()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()


class _DetailsTextArea(ConfirmableTextArea):
    """``ConfirmableTextArea`` that bubbles ``alt+`` and ``ctrl+`` keys to the outer view so the
    parent's bindings (focus nav, lifecycle actions) win over the TextArea's default consumption."""

    def _on_key(self, event: Key) -> None:
        if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
            event.prevent_default()
            return
        super()._on_key(event)


class EntryDetails(Vertical):
    """View for ``EntryDetailsVM``. Subscribes to ``vm.dirty``; mirrors VM state into both
    TextAreas and toggles the choices' visibility based on ``is_dirty``."""

    DEFAULT_CSS = """
    EntryDetails {
        width: 2fr;
        height: auto;
        padding: 0 1;
    }
    EntryDetails #cp-details-title {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 0 1;
    }
    EntryDetails #cp-details-title:focus {
        border: solid $accent;
    }
    EntryDetails #cp-details-content {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 5;
        max-height: 12;
        padding: 0 1;
    }
    EntryDetails #cp-details-content:focus {
        border: solid $accent;
    }
    EntryDetails #cp-details-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryDetails #cp-details-choices.-visible {
        display: block;
    }
    EntryDetails #cp-details-choices:focus {
        border-top: solid $accent;
    }
    """

    def __init__(self, vm: EntryDetailsVM, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vm = vm
        self._was_dirty = False

    def compose(self):
        title = _DetailsTextArea(id="cp-details-title", show_line_numbers=False, soft_wrap=True)
        title.border_title = "Title"
        yield title
        content = _DetailsTextArea(id="cp-details-content", show_line_numbers=False, soft_wrap=True)
        content.border_title = "Content"
        yield content
        yield _EntryDetailChoices(self._vm, id="cp-details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        title_area = self.query_one("#cp-details-title", TextArea)
        content_area = self.query_one("#cp-details-content", TextArea)
        choices = self.query_one("#cp-details-choices", _EntryDetailChoices)

        if title_area.text != self._vm.title:
            title_area.text = self._vm.title
        if content_area.text != self._vm.content:
            content_area.text = self._vm.content

        is_dirty = self._vm.is_dirty
        if is_dirty:
            if not self._was_dirty:
                choices.prepare_for_show()
            choices.add_class("-visible")
        else:
            # Focus-orphan rescue (mirrors browser entry-details).
            if (
                self._was_dirty
                and self.screen is not None
                and self.screen.focused is choices
            ):
                content_area.focus()
            choices.remove_class("-visible")
        self._was_dirty = is_dirty

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        wid = event.text_area.id
        if wid == "cp-details-title":
            self._vm.set_title(event.text_area.text)
        elif wid == "cp-details-content":
            self._vm.set_content(event.text_area.text)

    def on_confirmable_text_area_accept_edits_requested(
        self, event: ConfirmableTextArea.AcceptEditsRequested
    ) -> None:
        if self._vm.is_dirty:
            self._vm.accept()

    # ------------------------------------------------------------------
    # Boundary nav — bubble plain arrows on the title/content TextAreas. The TextArea consumes
    # arrows for caret movement; we don't override that. Instead the parent watches focus and uses
    # alt+arrow exclusively for inter-region nav.
    # ------------------------------------------------------------------

    def on_key(self, event: Key) -> None:
        # Escape inside title/content discards the buffer edit (reset to stored), matching the
        # ``esc auto-reset`` requirement.
        if event.key == "escape" and self._vm.is_dirty:
            self._vm.cancel()
            event.stop()
