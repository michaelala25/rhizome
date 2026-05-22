"""EntryDetailsViewModel + EntryDetailsView — the title/content side panel
that sits to the right of the entry table in
``KnowledgeEntryBrowserPaneView``.

Editing model: **buffered with explicit accept/cancel**. The title input
and content textarea write to in-VM buffers (``_title_buffer``,
``_content_buffer``) that are seeded from the entry on ``set_entry``. As
soon as either buffer diverges from the entry's stored value the VM
flips into a dirty state and the view reveals a two-line choices list
("Accept" / "Cancel") below the content area. The user navigates that
with arrows and confirms with enter, which either calls
``update_entry`` + commits + mutates the in-memory entry in place
(Accept) or resets the buffers (Cancel).

Cursor-move-while-dirty policy: **silent discard**. ``set_entry`` is
called by the pane VM on every cursor move; it reseeds the buffers from
the new entry, so any unsaved edits to the previous entry are lost. The
user must explicitly Accept before moving on.

The VM emits two distinct callback groups:

  * ``dirty`` — the usual repaint signal (buffer changed, entry changed,
    choice cursor moved, accept/cancel landed).
  * ``saved`` — fires only on successful Accept. The pane VM subscribes
    so it can repaint its table row (the in-memory ``KnowledgeEntry``
    was mutated in place, but the ``DataTable`` doesn't know that yet).

The VM is still a leaf — no subscriptions of its own. The pane VM is
the only writer (it calls ``set_entry``); the view drives the buffer
mutators and the accept/cancel actions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rich.text import Text

from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from rhizome.db import KnowledgeEntry
from rhizome.db.operations import update_entry
from rhizome.logs import get_logger

from ..view_model_base import ViewModelBase

_logger = get_logger("browser.entry_details")


class EntryDetailsViewModel(ViewModelBase):
    """Buffered-edit VM for the entry detail panel.

    Holds the current entry plus per-field buffers; flips into a dirty
    state when either buffer diverges from the entry's stored value.
    Accept/Cancel are the explicit exits from dirty. Until the user
    Accepts, nothing reaches the DB.
    """

    class Callbacks(Enum):
        # Standard dirty + focus inherited. ``SAVED`` is browser-specific:
        # the pane VM subscribes so it can repaint its table row after a
        # successful write.
        SAVED = "saved"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._saved = self._make_group(EntryDetailsViewModel.Callbacks.SAVED)

        self._entry: KnowledgeEntry | None = None
        # Buffers shadow the entry's stored values. Seeded on every
        # ``set_entry`` so the dirty test (buffer != entry.field) is a
        # plain string compare with no extra state.
        self._title_buffer: str = ""
        self._content_buffer: str = ""

        # 0 = Accept, 1 = Cancel. Reset on every entry change and on
        # accept/cancel completion. Modulo-2 wrap on arrow nav.
        self._choice_cursor: int = 0

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def saved(self):
        return self._saved

    @property
    def entry(self) -> KnowledgeEntry | None:
        return self._entry

    @property
    def title(self) -> str:
        """The *buffer* — i.e. what the user is currently editing. The view
        binds the title ``Input`` to this, not to the entry's stored value."""
        return self._title_buffer

    @property
    def content(self) -> str:
        """Buffer; same rationale as ``title``."""
        return self._content_buffer

    @property
    def original_title(self) -> str:
        return "" if self._entry is None else self._entry.title

    @property
    def original_content(self) -> str:
        return "" if self._entry is None else self._entry.content

    @property
    def is_dirty(self) -> bool:
        """True when either buffer diverges from the entry's stored value.
        Always False when there's no entry (nothing to edit)."""
        if self._entry is None:
            return False
        return (
            self._title_buffer != self._entry.title
            or self._content_buffer != self._entry.content
        )

    @property
    def choice_cursor(self) -> int:
        return self._choice_cursor

    # ------------------------------------------------------------------
    # Mutators (display side — called by the pane VM)
    # ------------------------------------------------------------------

    def set_entry(self, entry: KnowledgeEntry | None) -> None:
        """Switch the panel to display ``entry``. Reseeds the buffers from
        the new entry's stored values, silently discarding any in-flight
        edits to the previous entry (per the cursor-move-while-dirty
        policy). Identity check rather than equality so the same entry
        re-shown across two ``_sync_details`` calls is a no-op."""
        if self._entry is entry:
            return
        self._entry = entry
        self._title_buffer = "" if entry is None else entry.title
        self._content_buffer = "" if entry is None else entry.content
        self._choice_cursor = 0
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Mutators (edit side — called by the view's change handlers)
    # ------------------------------------------------------------------

    def set_title(self, value: str) -> None:
        """Update the title buffer. No-op when ``value`` already matches —
        absorbs the round-trip from the view's own ``input.value =`` and
        keeps stale ``Input.Changed`` events (see the view) from emitting
        spurious dirties."""
        if self._entry is None:
            return
        if value == self._title_buffer:
            return
        self._title_buffer = value
        self.emit(self.dirty)

    def set_content(self, value: str) -> None:
        """Update the content buffer. See ``set_title`` for the no-op
        rationale."""
        if self._entry is None:
            return
        if value == self._content_buffer:
            return
        self._content_buffer = value
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Choice cursor + accept/cancel
    # ------------------------------------------------------------------

    def move_choice_cursor(self, direction: int) -> None:
        """Move the Accept/Cancel cursor. No-op when the choices list isn't
        meaningful (clean state)."""
        if not self.is_dirty:
            return
        new = (self._choice_cursor + direction) % 2
        if new == self._choice_cursor:
            return
        self._choice_cursor = new
        self.emit(self.dirty)

    async def accept(self) -> None:
        """Persist the current buffers to the DB and mutate the in-memory
        entry in place.

        Mutating the entry instance after the write means the pane VM's
        ``self._entries[i]`` reference picks up the new values for free
        — no refetch needed. We then emit ``saved`` so the pane VM can
        repaint its table row with the new title. After this returns
        ``is_dirty`` is False and the choices list disappears naturally
        on the next refresh.

        No-op when there's nothing dirty to save (defensive — the choice
        confirm path is guarded by visibility, but a stray binding could
        still fire here)."""
        if self._entry is None or not self.is_dirty:
            return
        async with self._session_factory() as session:
            await update_entry(
                session,
                self._entry.id,
                title=self._title_buffer,
                content=self._content_buffer,
            )
            await session.commit()
        # Bring the in-memory entry in sync with the persisted values
        # *after* the commit so any view subscribers seeing ``saved`` can
        # trust ``entry.title`` / ``entry.content``.
        self._entry.title = self._title_buffer
        self._entry.content = self._content_buffer
        self._choice_cursor = 0
        self.emit(self.dirty)
        self.emit(self._saved)

    def cancel(self) -> None:
        """Discard the buffers and return to the entry's stored values."""
        if self._entry is None or not self.is_dirty:
            return
        self._title_buffer = self._entry.title
        self._content_buffer = self._entry.content
        self._choice_cursor = 0
        self.emit(self.dirty)


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
