"""``TopicDetails`` (+ private ``TopicDetailChoices``) — name/description side panel under the
topic tree in the browser's left rail. See ``view_model.py`` for the VM contract."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from textual import on
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from rhizome.tui.widgets.shared.choices_list import ChoiceList
from rhizome.tui.widgets.shared.text_area import ConfirmableTextArea
from rhizome.app.browser.topics.details import TopicDetailsModel


# Dim grey for labels; values render in default fg so they read as the "data".
_LABEL_STYLE = "rgb(120,120,120)"


class TopicDetailChoices(ChoiceList[TopicDetailsModel]):
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


class TopicDetails(Vertical):
    """View for ``TopicDetailsModel``: name ``TextArea`` over description ``TextArea`` over
    hidden-when-clean ``TopicDetailChoices``.

    Subscribes to ``vm.dirty`` and mirrors VM state into all three widgets each refresh, with an
    equality guard on each assignment so we don't trigger spurious ``TextArea.Changed`` round-trips.
    Detail widgets render an "(no topic)" placeholder header when no topic is loaded.
    """

    DEFAULT_CSS = """
    TopicDetails {
        height: auto;
        padding: 0 1;
        border-top: solid #3a3a3a;
    }
    TopicDetails #topic-details-name {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 5;
        padding: 0 1;
        margin: 1 0 0 0;
    }
    TopicDetails #topic-details-name:focus {
        border: solid $accent;
    }
    TopicDetails #topic-details-description {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 4;
        max-height: 12;
        padding: 0 1;
    }
    TopicDetails #topic-details-description:focus {
        border: solid $accent;
    }
    TopicDetails #topic-details-counts {
        height: auto;
        padding: 0 1;
    }
    TopicDetails #topic-details-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    TopicDetails #topic-details-choices.-visible {
        display: block;
    }
    TopicDetails #topic-details-choices:focus {
        border-top: solid $accent;
    }
    """

    def __init__(
        self,
        view_model: TopicDetailsModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``is_dirty`` so ``_refresh`` can detect transitions: clean→dirty
        # (reset the choice cursor) and dirty→clean (rescue focus before the choices widget hides).
        self._was_dirty: bool = False

    def compose(self):
        name = ConfirmableTextArea(
            id="topic-details-name", show_line_numbers=False, soft_wrap=True,
        )
        name.border_title = "Title"
        yield name
        desc = ConfirmableTextArea(
            id="topic-details-description", show_line_numbers=False, soft_wrap=True,
        )
        desc.border_title = "Description"
        yield desc
        yield Static("", id="topic-details-counts")
        yield TopicDetailChoices(self._vm, id="topic-details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        name_area = self.query_one("#topic-details-name", TextArea)
        desc_area = self.query_one("#topic-details-description", TextArea)
        choices = self.query_one("#topic-details-choices", TopicDetailChoices)

        target_name = self._vm.name
        target_desc = self._vm.description

        if name_area.text != target_name:
            name_area.text = target_name
        if desc_area.text != target_desc:
            desc_area.text = target_desc

        self.query_one("#topic-details-counts", Static).update(self._render_counts())

        # No topic loaded → read-only and clear. Equality-guarded so we don't fight the user when a
        # topic is live.
        no_topic = self._vm.topic is None
        if name_area.read_only != no_topic:
            name_area.read_only = no_topic
        if desc_area.read_only != no_topic:
            desc_area.read_only = no_topic

        is_dirty_now = self._vm.is_dirty
        if is_dirty_now:
            if not self._was_dirty:
                choices.prepare_for_show()
            choices.add_class("-visible")
        else:
            # Focus-orphan rescue: on dirty→clean, if focus is on the choices widget it's about to
            # be ``display: none``'d — move it to the description field first.
            if (
                self._was_dirty
                and self.screen is not None
                and self.screen.focused is choices
            ):
                desc_area.focus()
            choices.remove_class("-visible")
        self._was_dirty = is_dirty_now

    def _render_counts(self) -> Text:
        if self._vm.topic is None:
            return Text("", style=_LABEL_STYLE)
        text = Text()
        text.append("entries: ", style=_LABEL_STYLE)
        text.append(str(self._vm.direct_entries))
        text.append(f" ({self._vm.subtree_entries} in subtree)", style=_LABEL_STYLE)
        text.append("\n")
        text.append("flashcards: ", style=_LABEL_STYLE)
        text.append(str(self._vm.direct_flashcards))
        text.append(f" ({self._vm.subtree_flashcards} in subtree)", style=_LABEL_STYLE)
        return text

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        wid = event.text_area.id
        if wid == "topic-details-name":
            self._vm.set_name(event.text_area.text)
        elif wid == "topic-details-description":
            self._vm.set_description(event.text_area.text)

    @on(ConfirmableTextArea.AcceptEditsRequested)
    async def _on_accept_edits_requested(
        self, event: ConfirmableTextArea.AcceptEditsRequested
    ) -> None:
        if self._vm.is_dirty:
            await self._vm.accept()
