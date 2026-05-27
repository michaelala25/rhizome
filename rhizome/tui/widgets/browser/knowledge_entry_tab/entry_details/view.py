"""EntryDetailsView (+ private ``_ChoicesList``) — the title/content side panel that sits to the right of
the entry table in ``KnowledgeEntryBrowserTabView``. See ``view_model.py`` for the VM contract.
"""

from __future__ import annotations

from typing import Any

from textual.containers import Vertical
from textual.widgets import TextArea

from ...choices import ChoiceList
from .view_model import EntryDetailsViewModel


class _ChoicesList(ChoiceList[EntryDetailsViewModel]):
    """Horizontal Accept/Cancel choices for committing or discarding a details edit. Visible
    only while the parent view's ``vm.is_dirty`` is True; hidden via the ``.-visible`` CSS
    class managed by the parent's ``_refresh``. Escape always cancels regardless of cursor —
    matches the relink Accept/Cancel widget on the linked-flashcards panel.
    """

    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Edit: "
    HINT = "← / → move • enter confirm • esc cancels"

    async def _accept(self) -> None:
        await self._vm.accept()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()


class EntryDetailsView(Vertical):
    """View for ``EntryDetailsViewModel``. Title ``Input`` over a content ``TextArea`` over a
    hidden-when-clean choices list.

    Subscribes to ``vm.dirty`` and mirrors VM state into all three widgets each refresh, guarding each
    assignment with a value-equality check so we don't trigger unnecessary ``Changed`` events (which Textual
    dispatches async and which we'd otherwise have to filter back out — see ``on_input_changed`` for the
    stale-event filter that handles the residual case).
    """

    DEFAULT_CSS = """
    EntryDetailsView {
        height: 1fr;
        padding: 0 1;
    }
    EntryDetailsView #details-title {
        background: transparent;
        border: solid #3a3a3a;
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 0 1;
        margin: 0 0 0 0;
    }
    EntryDetailsView #details-title:focus {
        border: solid $accent;
    }
    EntryDetailsView #details-content {
        background: transparent;
        border: solid #3a3a3a;
        height: 1fr;
        padding: 0 1;
    }
    EntryDetailsView #details-content:focus {
        border: solid $accent;
    }
    EntryDetailsView #details-choices {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryDetailsView #details-choices.-visible {
        display: block;
    }
    EntryDetailsView #details-choices:focus {
        border-top: solid $accent;
    }
    """

    def __init__(
        self,
        view_model: EntryDetailsViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``is_dirty`` so ``_refresh`` can detect the dirty→clean transition and rescue
        # focus from the about-to-hide choices widget. Without this Textual leaves ``screen.focused`` on a
        # ``display: none`` widget and the next keystroke goes nowhere visible.
        self._was_dirty: bool = False

    def compose(self):
        # Both title and content are ``TextArea`` so long titles wrap rather than overflowing horizontally.
        # ``soft_wrap=True`` is the default but we name it for clarity; ``show_line_numbers=False`` keeps
        # both fields looking like editable boxes rather than code editors.
        yield TextArea(
            id="details-title", show_line_numbers=False, soft_wrap=True,
        )
        yield TextArea(
            id="details-content", show_line_numbers=False, soft_wrap=True,
        )
        yield _ChoicesList(self._vm, id="details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # Paint whatever the VM was holding at construction (typically nothing, but the tab VM may have
        # called ``set_entry`` before mount).
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        title_area = self.query_one("#details-title", TextArea)
        content_area = self.query_one("#details-content", TextArea)
        choices = self.query_one("#details-choices", _ChoicesList)

        target_title = self._vm.title
        target_content = self._vm.content

        # Equality-guard each assignment — both because Textual's ``Changed`` events are cheap-but-not-free
        # and because we want to minimize the round-trip into our own change handlers.
        if title_area.text != target_title:
            title_area.text = target_title
        if content_area.text != target_content:
            content_area.text = target_content

        # Freeze the edit surfaces while the tab is in multi-select mode. The cursor still moves through
        # entries so we keep the text current, but ``read_only=True`` blocks user keystrokes and we hide the
        # Accept/Cancel choices entirely. ``is_dirty`` is treated as effectively False from the view's
        # perspective — there's no way to act on the buffers — so any stale buffer divergence carried into
        # multi-select mode is invisible to the user. (It'll be reseeded on the next ``set_entry`` from the
        # tab VM's normal cursor sync.)
        frozen = self._vm.multi_select_active
        if title_area.read_only != frozen:
            title_area.read_only = frozen
        if content_area.read_only != frozen:
            content_area.read_only = frozen

        is_dirty_now = self._vm.is_dirty and not frozen
        if is_dirty_now:
            # Reset the choice cursor on each clean→dirty transition so each fresh open lands on
            # Accept regardless of where the user left it previously.
            if not self._was_dirty:
                choices.prepare_for_show()
            choices.add_class("-visible")
        else:
            # On the dirty→clean (or dirty→frozen) transition, if focus was on the choices widget it's about
            # to be display:none'd — move it back to the content area first so the user lands somewhere
            # sensible. Frozen also lands here, but the parent tab's focus guard generally keeps focus on
            # the table while frozen, so this branch is belt-and-braces.
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
        # Both title and content are ``TextArea``s — dispatch by id. No stale-event filter needed
        # (``TextArea.Changed`` carries no snapshotted text field — the handler reads ``text_area.text``
        # live, which always reflects the latest synchronous assignment). The VM mutators' equality
        # early-return absorbs the round-trip from our own ``_refresh`` assignments.
        wid = event.text_area.id
        if wid == "details-title":
            self._vm.set_title(event.text_area.text)
        elif wid == "details-content":
            self._vm.set_content(event.text_area.text)
