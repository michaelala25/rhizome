"""``EntryDetails`` (+ private ``EntryDetailChoices``) — title/content side panel to the right of the
entry table in ``EntryTab``. See ``view_model.py`` for the VM contract.
"""

from __future__ import annotations

from typing import Any

from textual.containers import Vertical
from textual.widgets import TextArea

from rhizome.tui.widgets.browser.shared.choices_list import ChoiceList
from rhizome.tui.widgets.browser.shared.confirmable_text_area import ConfirmableTextArea
from rhizome.app.browser.tabs.entries.entry_details import EntryDetailsVM


class EntryDetailChoices(ChoiceList[EntryDetailsVM]):
    """Horizontal Accept/Cancel for committing or discarding a details edit. Visible only while
    ``vm.is_dirty`` (parent toggles the ``.-visible`` class in its ``_refresh``). Escape always
    cancels regardless of cursor position."""

    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Edit: "
    HINT = "ctrl+enter to accept"

    async def _accept(self) -> None:
        await self._vm.accept()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()


class EntryDetails(Vertical):
    """View for ``EntryDetailsVM``: title ``TextArea`` over content ``TextArea`` over
    hidden-when-clean ``EntryDetailChoices``.

    Subscribes to ``vm.dirty`` and mirrors VM state into all three widgets each refresh, with an
    equality guard on each assignment so we don't trigger spurious ``TextArea.Changed`` round-trips.
    """

    DEFAULT_CSS = """
    EntryDetails {
        height: 1fr;
        padding: 0 1;
    }
    EntryDetails #details-title {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 0 1;
        margin: 0 0 0 0;
    }
    EntryDetails #details-title:focus {
        border: solid $accent;
    }
    EntryDetails #details-content {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: 1fr;
        padding: 0 1;
    }
    EntryDetails #details-content:focus {
        border: solid $accent;
    }
    EntryDetails #details-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryDetails #details-choices.-visible {
        display: block;
    }
    EntryDetails #details-choices:focus {
        border-top: solid $accent;
    }
    """

    def __init__(
        self,
        view_model: EntryDetailsVM,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``is_dirty`` so ``_refresh`` can detect transitions: clean→dirty (reset
        # the choice cursor) and dirty→clean (rescue focus before the choices widget is hidden).
        self._was_dirty: bool = False

    def compose(self):
        # Title is a ``TextArea`` so long titles wrap rather than overflowing. ``show_line_numbers=False``
        # keeps both fields looking like editable boxes rather than code editors.
        title = ConfirmableTextArea(
            id="details-title", show_line_numbers=False, soft_wrap=True,
        )
        title.border_title = "Title"
        yield title
        content = ConfirmableTextArea(
            id="details-content", show_line_numbers=False, soft_wrap=True,
        )
        content.border_title = "Content"
        yield content
        yield EntryDetailChoices(self._vm, id="details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        title_area = self.query_one("#details-title", TextArea)
        content_area = self.query_one("#details-content", TextArea)
        choices = self.query_one("#details-choices", EntryDetailChoices)

        target_title = self._vm.title
        target_content = self._vm.content

        if title_area.text != target_title:
            title_area.text = target_title
        if content_area.text != target_content:
            content_area.text = target_content

        # Multi-select freeze: cursor still moves entries through (text stays current) but keystrokes
        # are blocked and the choices stay hidden. Any stale buffer divergence is invisible since
        # ``is_dirty`` is treated as False below, and the next ``set_entry`` reseeds anyway.
        frozen = self._vm.multi_select_active
        if title_area.read_only != frozen:
            title_area.read_only = frozen
        if content_area.read_only != frozen:
            content_area.read_only = frozen

        is_dirty_now = self._vm.is_dirty and not frozen
        if is_dirty_now:
            # On clean→dirty, reset the choice cursor so each fresh open lands on Accept.
            if not self._was_dirty:
                choices.prepare_for_show()
            choices.add_class("-visible")
        else:
            # Focus-orphan rescue: on dirty→clean, if focus is on the choices widget it's about to be
            # ``display: none``'d — move it to the content area first so the user lands somewhere
            # sensible. (Frozen also lands here, but the tab usually parks focus on the table while
            # frozen, so this is belt-and-braces.)
            if (
                self._was_dirty
                and self.screen is not None
                and self.screen.focused is choices
            ):
                content_area.focus()
            choices.remove_class("-visible")
        self._was_dirty = is_dirty_now

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Dispatch by id since both fields are ``TextArea``s. ``text_area.text`` is read live, and the
        # VM mutators' equality guards absorb the round-trip from our own ``_refresh`` assignments.
        wid = event.text_area.id
        if wid == "details-title":
            self._vm.set_title(event.text_area.text)
        elif wid == "details-content":
            self._vm.set_content(event.text_area.text)

    async def on_confirmable_text_area_accept_edits_requested(
        self, event: ConfirmableTextArea.AcceptEditsRequested
    ) -> None:
        if self._vm.is_dirty and not self._vm.multi_select_active:
            await self._vm.accept()
