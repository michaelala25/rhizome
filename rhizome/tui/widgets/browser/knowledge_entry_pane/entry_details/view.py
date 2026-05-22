"""EntryDetailsView (+ private ``_ChoicesList``) — the title/content side
panel that sits to the right of the entry table in
``KnowledgeEntryBrowserPaneView``. See ``view_model.py`` for the VM
contract.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from .view_model import EntryDetailsViewModel


class _ChoicesList(Static, can_focus=True):
    """Two-line Accept/Cancel choices list. Focusable so up/down/enter
    bindings can fire only when the user has explicitly given it focus
    (avoids hijacking those keys from the title input or content area).

    Owns its own render — both because the focus state (which affects
    cursor brightness) lives here, not on the VM, and because keeping
    the render co-located with the widget keeps the parent view's
    ``_refresh`` simpler (it only has to toggle the ``-visible`` class).
    Subscribes to ``vm.dirty`` for choice-cursor moves and to its own
    focus/blur events for the brightness change.
    """

    BINDINGS = [
        Binding("up", "choice_up", show=False),
        Binding("down", "choice_down", show=False),
        Binding("enter", "choice_confirm", show=False),
    ]

    def __init__(self, view_model: EntryDetailsViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Focus state changes the cursor brightness — re-render. (We
        # can't drive this from a CSS ``:focus`` rule because the
        # rendered ``Text`` carries its own per-segment styles that
        # would override widget-level colour.)
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_choices())

    def _render_choices(self) -> Text:
        """Two lines: ``► Accept`` / ``  Cancel`` (or vice versa). Cursor
        brightness tracks focus — bright on focus, dim grey otherwise.
        Label styling tracks the *selected* state and is independent of
        focus."""
        labels = ("Accept", "Cancel")
        cursor_style = "bold" if self.has_focus else "#6a6a6a"
        text = Text()
        for i, label in enumerate(labels):
            selected = i == self._vm.choice_cursor
            if selected:
                text.append("► ", style=cursor_style)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
            if i < len(labels) - 1:
                text.append("\n")
        return text

    def action_choice_up(self) -> None:
        self._vm.move_choice_cursor(-1)

    def action_choice_down(self) -> None:
        self._vm.move_choice_cursor(1)

    async def action_choice_confirm(self) -> None:
        # Dispatch by current cursor position. ``accept`` is async (it
        # opens a session and commits); ``cancel`` is sync. Textual
        # supports async actions, so this signature is fine.
        if self._vm.choice_cursor == 0:
            await self._vm.accept()
        else:
            self._vm.cancel()


class EntryDetailsView(Vertical):
    """View for ``EntryDetailsViewModel``. Title ``Input`` over a content
    ``TextArea`` over a hidden-when-clean choices list.

    Subscribes to ``vm.dirty`` and mirrors VM state into all three
    widgets each refresh, guarding each assignment with a value-equality
    check so we don't trigger unnecessary ``Changed`` events (which
    Textual dispatches async and which we'd otherwise have to filter
    back out — see ``on_input_changed`` for the stale-event filter that
    handles the residual case).
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
        margin: 0 0 1 0;
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
        height: 2;
        margin: 1 0 0 0;
        padding: 0 1;
        color: rgb(150,150,150);
        display: none;
    }
    EntryDetailsView #details-choices.-visible {
        display: block;
    }
    """

    # Sub-region cycle for cross-region focus nav (alt+left/right driven
    # from ``BrowserView``). Ordered left-to-right / top-to-bottom in
    # display order; the choices entry is skipped when its widget is
    # hidden (clean state).
    _REGION_IDS = ("details-title", "details-content", "details-choices")

    def __init__(
        self,
        view_model: EntryDetailsViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``is_dirty`` so ``_refresh`` can detect the
        # dirty→clean transition and rescue focus from the about-to-hide
        # choices widget. Without this Textual leaves ``screen.focused``
        # on a ``display: none`` widget and the next keystroke goes
        # nowhere visible.
        self._was_dirty: bool = False

    def compose(self):
        # Both title and content are ``TextArea`` so long titles wrap
        # rather than overflowing horizontally. ``soft_wrap=True`` is the
        # default but we name it for clarity; ``show_line_numbers=False``
        # keeps both fields looking like editable boxes rather than code
        # editors.
        yield TextArea(
            id="details-title", show_line_numbers=False, soft_wrap=True,
        )
        yield TextArea(
            id="details-content", show_line_numbers=False, soft_wrap=True,
        )
        yield _ChoicesList(self._vm, id="details-choices")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # Paint whatever the VM was holding at construction (typically
        # nothing, but the pane VM may have called ``set_entry`` before
        # mount).
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

        # Equality-guard each assignment — both because Textual's
        # ``Changed`` events are cheap-but-not-free and because we want
        # to minimize the round-trip into our own change handlers.
        if title_area.text != target_title:
            title_area.text = target_title
        if content_area.text != target_content:
            content_area.text = target_content

        is_dirty_now = self._vm.is_dirty
        if is_dirty_now:
            choices.add_class("-visible")
        else:
            # On the dirty→clean transition (Accept/Cancel just landed),
            # if focus was on the choices widget it's about to be
            # display:none'd — move it back to the content area first so
            # the user lands somewhere sensible.
            if (
                self._was_dirty
                and self.screen is not None
                and self.screen.focused is choices
            ):
                content_area.focus()
            choices.remove_class("-visible")
        self._was_dirty = is_dirty_now

    # ------------------------------------------------------------------
    # Cross-region focus (driven by parent pane's alt+left/right)
    # ------------------------------------------------------------------
    #
    # Internal cycle through ``_REGION_IDS``. The choices region is
    # skipped when its widget is hidden (``widget.display`` is False
    # while the ``-visible`` class is absent). Methods return True if
    # they successfully moved focus, False if they were already at the
    # corresponding edge — the parent pane uses the bool to decide
    # whether to step further (e.g. back to the table).

    def focus_first(self) -> None:
        """Land on the leftmost sub-region (title). Called by the parent
        pane when ``BrowserView`` enters the details region from the
        left."""
        self.query_one("#details-title", TextArea).focus()

    def focus_next_region(self) -> bool:
        cur = self._current_region_index()
        if cur is None:
            self.focus_first()
            return True
        for i in range(cur + 1, len(self._REGION_IDS)):
            widget = self.query_one(f"#{self._REGION_IDS[i]}")
            if not widget.display:
                continue
            widget.focus()
            return True
        return False

    def focus_prev_region(self) -> bool:
        cur = self._current_region_index()
        if cur is None:
            return False
        for i in range(cur - 1, -1, -1):
            widget = self.query_one(f"#{self._REGION_IDS[i]}")
            if not widget.display:
                continue
            widget.focus()
            return True
        return False

    def _current_region_index(self) -> int | None:
        """Locate the focused widget within ``_REGION_IDS``. Returns the
        index, or ``None`` if focus is outside the details panel."""
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return None
        for i, wid in enumerate(self._REGION_IDS):
            try:
                widget = self.query_one(f"#{wid}")
            except Exception:
                continue
            if focused is widget:
                return i
        return None

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Both title and content are ``TextArea``s — dispatch by id. No
        # stale-event filter needed (``TextArea.Changed`` carries no
        # snapshotted text field — the handler reads ``text_area.text``
        # live, which always reflects the latest synchronous assignment).
        # The VM mutators' equality early-return absorbs the round-trip
        # from our own ``_refresh`` assignments.
        wid = event.text_area.id
        if wid == "details-title":
            self._vm.set_title(event.text_area.text)
        elif wid == "details-content":
            self._vm.set_content(event.text_area.text)
