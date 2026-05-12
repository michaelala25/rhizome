"""CommitProposal view — owns layout, focus routing, and key bindings.

Talks to ``CommitProposalViewModel`` through plain method calls; subscribes to ``vm.dirty`` for repaints.
The VM has no knowledge of which Textual widget is focused, which key fires which action, or which
choices are visible — all of that is decided here.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Static, TextArea

from ..entry_list import ENTRY_ACCENT, ENTRY_DIM, ENTRY_HINT
from ..interrupt import InterruptWidgetBase
from ..view_base import ViewBase
from .view_model import CommitProposalViewModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


# ========================================================================================================================
# Style constants & static text
# ========================================================================================================================

_RED = ENTRY_ACCENT
_DIM = ENTRY_DIM
_HINT = ENTRY_HINT
_EXCLUDED = "rgb(60,60,60)"
_FOCUS_GREEN = "rgb(100,200,100)"

# Resolution-line colors — match FlashcardReview's ``Session complete`` / ``Session cancelled`` palette
# so the two widgets read as a family when they appear in the same chat transcript.
_DONE_GREEN = "rgb(120,210,110)"
_CANCEL_RED = "rgb(235,100,100)"

_HINTS_TEXT = (
    "  d: exclude/include  "
    "f: cycle type  "
    "t: change topic  "
    "alt+t: change all topics  "
    "alt+←/→: cycle fields"
)

_EDIT_INSTRUCTIONS_TITLE = "alt+e to toggle  ·  esc esc to discard"

_CHOICE_APPROVE = "approve"
_CHOICE_REQUEST_EDITS = "request_edits"
_CHOICE_DISMISS_EDITS = "dismiss_edits"
_CHOICE_RESET = "reset"
_CHOICE_CANCEL = "cancel"

_CHOICE_LABELS: dict[str, str] = {
    _CHOICE_APPROVE: "Approve",
    _CHOICE_REQUEST_EDITS: "Edit",
    _CHOICE_DISMISS_EDITS: "Dismiss edits",
    _CHOICE_RESET: "Reset",
    _CHOICE_CANCEL: "Cancel",
}

_CHOICE_HINTS: dict[str, str] = {
    _CHOICE_APPROVE: "ctrl+a",
    _CHOICE_REQUEST_EDITS: "ctrl+e",
    _CHOICE_DISMISS_EDITS: "ctrl+e",
    _CHOICE_RESET: "ctrl+r",
    _CHOICE_CANCEL: "esc",
}

_CHOICE_DESCRIPTIONS: dict[str, str] = {
    _CHOICE_APPROVE: "accept the proposal (including any changes made above)",
    _CHOICE_REQUEST_EDITS: "describe the changes you'd like to make",
    _CHOICE_DISMISS_EDITS: "hide the edit-instructions area without discarding it",
    _CHOICE_RESET: "discard all changes and restore the original proposal",
    _CHOICE_CANCEL: "cancel the proposal",
}

# Max gap (seconds) for the discard-edits chord — matches chat_input.
_DOUBLE_ESC_WINDOW = 0.5


# ========================================================================================================================
# Child editors
# ========================================================================================================================
#
# All three need ctrl+*/alt+* keys to escape their default handling so the outer widget's bindings can
# fire. ``_EditInstructions`` additionally bubbles ``up`` at row 0 and ``escape`` so the parent can
# route those (jump to choices, double-tap discard), and posts ``EditInstructionsSubmit`` on enter so
# the parent can route to ``vm.accept`` — same enter/ctrl+enter convention as ``chat_input``.
#
# ------------------------------------------------------------------------------------------------------------------------


class EditInstructionsSubmit(Message):
    """Posted by ``_EditInstructions`` when the user presses Enter to submit (matching chat_input's
    enter-submits / ctrl+enter-newline convention). The owning ``CommitProposal`` translates this
    into ``vm.accept()``, equivalent to picking "Approve" from the choices list. Lives at module
    scope (rather than nested on ``_EditInstructions``) so the handler name on the owner reads as
    a plain ``on_edit_instructions_submit`` without a leading double underscore."""


def _bubble_app_keys(event: events.Key) -> bool:
    if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
        event.prevent_default()
        return True
    return False


class _TitleInput(Input):
    def _on_key(self, event: events.Key) -> None:
        if not _bubble_app_keys(event):
            super()._on_key(event)


class _ContentArea(TextArea):
    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if not _bubble_app_keys(event):
            super()._on_key(event)


class _ChoicesList(Static, can_focus=True):
    """Focusable list of actions. Pure data widget — holds the cursor and its own focused flag; all key
    handling is centralised on the parent's ``on_key`` so navigation between regions lives in one place.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cursor: int | None = None


class _EditInstructions(TextArea):
    """Lets ctrl+*/alt+* keys bubble (so global bindings still fire), and always bubbles ``escape`` so the
    parent can run the double-tap discard chord. The ``up``-at-(0,0) escape is handled by gating our local
    ``up`` binding through ``check_action`` — at the very top-left we return False, marking the binding
    inactive so Textual continues walking the DOM and fires ``CommitProposal``'s ``up`` binding (which
    steps focus to the choices list). Anywhere else, we return True; the binding is "active" but has no
    matching ``action_cursor_up`` on this widget, so Textual falls through to ``TextArea``'s inherited
    ``up`` binding for normal cursor movement."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        # Submit / newline overrides — mirror ``chat_input``. Order matters: ``ctrl+j`` is what the
        # terminal delivers for Ctrl+Enter in most setups, so we have to intercept it before the
        # generic ``_bubble_app_keys`` swallows everything starting with ``ctrl+``.
        if event.key == "enter":
            # Default TextArea behavior inserts a newline; we want enter to submit instead. Hand off
            # to the parent via a typed message rather than walking the DOM here.
            self.post_message(EditInstructionsSubmit())
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return

        if _bubble_app_keys(event):
            return
        if event.key == "escape":
            event.prevent_default()
            return
        super()._on_key(event)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Returning False here marks the binding inactive on this widget, which causes Textual to keep
        # walking the DOM and let the parent's ``up`` binding fire — that's how we route out to choices.
        # Everywhere else (cursor not at (0,0)) we leave the binding "active"; since this widget has no
        # ``action_cursor_up``, the lookup fails locally and Textual falls through to ``TextArea``'s
        # inherited ``up`` binding for cursor movement.
        if action == "cursor_up":
            return self.cursor_location != (0, 0)
        return super().check_action(action, parameters)


# ========================================================================================================================
# CommitProposal
# ========================================================================================================================


class CommitProposal(
    ViewBase[CommitProposalViewModel],
    InterruptWidgetBase,
    can_focus=True,
):

    DISABLE_CHILDREN_ON_DEACTIVATE = False

    # Region-cycle targets, in display order.
    _CYCLE_TARGETS = ("outer", "cp-detail-title", "cp-detail-content")

    BINDINGS = [
        Binding("up,k", "cursor_up", show=False),
        Binding("down,j", "cursor_down", show=False),
        Binding("escape", "escape_chord", show=False),
        Binding("f", "cycle_type('forward')", show=False),
        Binding("shift+f", "cycle_type('back')", show=False),
        Binding("d", "toggle_exclude", show=False),
        Binding("t", "pick_topic('current')", show=False),
        Binding("alt+t", "pick_topic('all')", show=False),
        Binding("alt+right", "cycle_field('forward')", show=False),
        Binding("alt+left", "cycle_field('back')", show=False),
        Binding("ctrl+e", "toggle_edit_instructions", show=False),
        Binding("alt+e", "swap_edit_focus", show=False),
        Binding("ctrl+a", "accept", show=False),
        Binding("ctrl+r", "reset", show=False),
        Binding("enter", "select_choice", show=False),
        # Same key, different state: in DONE, ``enter`` toggles the collapse fold. Both bindings are
        # registered together — ``check_action`` is what picks which one fires based on ``vm.state``.
        # If the first ``select_choice`` binding's check_action returns False (DONE), Textual falls
        # through and tries the next, which is this one.
        Binding("enter", "toggle_collapsed", show=False),
    ]

    DEFAULT_CSS = """
    CommitProposal {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 1 0;
    }
    CommitProposal #cp-collapse {
        dock: right;
        width: auto;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: rgb(100,100,100);
        display: none;
    }
    CommitProposal #cp-collapse:hover {
        color: rgb(200,200,200);
    }
    CommitProposal #cp-collapse.-visible {
        display: block;
    }
    CommitProposal #cp-hints {
        color: rgb(80,80,80);
        margin-bottom: 1;
    }
    CommitProposal #cp-entry-list-scroll {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
        scrollbar-size-vertical: 1;
    }
    CommitProposal #cp-entry-list {
        height: auto;
    }
    CommitProposal #cp-detail {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    CommitProposal #cp-detail-title {
        background: transparent;
        border: none;
        height: 1;
        padding: 0;
        margin: 0 0 1 0;
    }
    CommitProposal #cp-detail-title:focus {
        border: solid $accent;
        height: 3;
    }
    CommitProposal #cp-detail-meta {
        color: rgb(100,100,100);
        height: 1;
        margin: 0 0 1 0;
    }
    CommitProposal #cp-detail-content {
        background: transparent;
        border: none;
        height: auto;
        min-height: 3;
        max-height: 12;
        padding: 0 1;
    }
    CommitProposal #cp-detail-content:focus {
        border: solid $accent;
    }
    CommitProposal #cp-choices {
        height: auto;
        color: rgb(150,150,150);
        margin-top: 1;
    }
    CommitProposal #cp-choices.-hidden {
        display: none;
    }
    CommitProposal #cp-edit-instructions {
        background: transparent;
        border: solid $surface-lighten-2;
        height: auto;
        min-height: 3;
        max-height: 8;
        margin: 1 0 0 0;
        padding: 0 1;
        display: none;
        border-title-align: right;
        border-title-color: rgb(80,80,80);
    }
    CommitProposal #cp-edit-instructions.-visible {
        display: block;
    }
    CommitProposal #cp-edit-instructions:focus {
        border: solid rgb(120,120,140);
    }
    CommitProposal #cp-resolution {
        height: 1;
        margin: 1 0 0 0;
        display: none;
    }
    CommitProposal #cp-resolution.-visible {
        display: block;
    }
    CommitProposal #cp-resolution.-centered {
        text-align: center;
    }
    CommitProposal #cp-resolution-edits {
        background: rgb(32,32,32);
        color: rgb(180,180,180);
        height: auto;
        padding: 1 2;
        margin: 1 0 0 0;
        display: none;
    }
    CommitProposal #cp-resolution-edits.-visible {
        display: block;
    }
    """

    # ========================================================================================================================
    # Construction
    # ========================================================================================================================

    @classmethod
    def from_interrupt(cls, value: dict[str, Any]) -> "CommitProposal":
        return cls(
            entries=value["entries"],
            topic_map=value.get("topic_map", {}),
            session_factory=value.get("session_factory"),
        )

    def __init__(
        self,
        entries: list[dict[str, Any]],
        topic_map: dict[int, str],
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
        **kwargs,
    ) -> None:
        super().__init__(
            CommitProposalViewModel(
                entries=entries,
                topic_map=topic_map,
                session_factory=session_factory,
            ),
            **kwargs,
        )
        self._session_factory = session_factory

        # Last-escape timestamp for the discard-edits double-tap chord.
        self._last_escape_at: float | None = None

        # Resolve the future when the VM reaches DONE. Doesn't query child widgets, so safe to subscribe
        # pre-mount; torn down in on_unmount.
        self._vm.subscribe(self._vm.dirty, self._maybe_resolve)


    def compose(self) -> ComposeResult:
        # ``cp-collapse`` is docked top-right by CSS, not laid out inline; the rest of the children
        # compose vertically below it. Only shown in DONE (see ``_refresh_collapse_button``).
        yield Button("▼", id="cp-collapse")
        yield Static("", id="cp-header")
        yield Static(Text(_HINTS_TEXT, style=_HINT), id="cp-hints")

        with VerticalScroll(id="cp-entry-list-scroll"):
            yield Static("", id="cp-entry-list")

        with Vertical(id="cp-detail"):
            yield _TitleInput(id="cp-detail-title", placeholder="(title)")
            yield Static("", id="cp-detail-meta")
            yield _ContentArea(id="cp-detail-content")

        yield _ChoicesList(id="cp-choices")
        yield _EditInstructions(id="cp-edit-instructions")

        # DONE-only siblings. ``cp-resolution`` shows a single-line status ("Accepted" / "Cancelled" /
        # "Accepted with edits:") and ``cp-resolution-edits`` is the read-only panel that displays the
        # user's submitted edit-instructions text — only visible when accepted with non-empty edits.
        # Both default to ``display: none`` in CSS; ``_refresh_resolution`` toggles ``.-visible``.
        yield Static("", id="cp-resolution")
        yield Static("", id="cp-resolution-edits")


    def on_mount(self) -> None:
        super().on_mount()

        self.query_one("#cp-edit-instructions", _EditInstructions).border_title = (
            _EDIT_INSTRUCTIONS_TITLE
        )

        self._refresh()

    def on_unmount(self) -> None:
        super().on_unmount()  # ViewBase tears down the dirty→_refresh / focus→self.focus subs
        self._vm.unsubscribe(self._vm.dirty, self._maybe_resolve)


    # Bindings that mutate VM state or trigger the resolve chain. Disabled in DONE so the parallel VM
    # asserts can stay strict — Textual will mark each one inactive while ``state != EDITING``, and the
    # widget itself remains focusable for post-resolve navigation.
    _EDITING_ONLY_ACTIONS = frozenset({
        "cycle_type",
        "cycle_field",
        "toggle_exclude",
        "pick_topic",
        "toggle_edit_instructions",
        "swap_edit_focus",
        "accept",
        "reset",
        "select_choice",
        "cancel_interrupt",
    })

    # Inverse — only valid once the widget is resolved. Currently just the collapse-fold toggle, which
    # is bound on ``enter`` alongside ``select_choice``; ``check_action`` picks the right one by state.
    _DONE_ONLY_ACTIONS = frozenset({
        "toggle_collapsed",
    })

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self._EDITING_ONLY_ACTIONS:
            return self._vm.state == CommitProposalViewModel.State.EDITING
        if action in self._DONE_ONLY_ACTIONS:
            return self._vm.state == CommitProposalViewModel.State.DONE
        return super().check_action(action, parameters)


    # ========================================================================================================================
    # Event Handling
    # ========================================================================================================================


    def on_input_changed(self, event: Input.Changed) -> None:
        # Echoes from ``_refresh_detail``'s programmatic writes also reach us; in DONE we drop them so
        # they don't trip the VM's EDITING-only asserts. Navigating across entries post-resolve still
        # rewrites the title field, but that's just paint, not a model change.
        if self._vm.state != CommitProposalViewModel.State.EDITING:
            return
        if event.input.id == "cp-detail-title":
            self._vm.set_entry_title(self._vm.cursor, event.value)


    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._vm.state != CommitProposalViewModel.State.EDITING:
            return
        wid = event.text_area.id
        if wid == "cp-detail-content" and self._vm.cursor is not None:
            self._vm.set_entry_content(self._vm.cursor, event.text_area.text)
        elif wid == "cp-edit-instructions":
            self._vm.set_edit_instructions(event.text_area.text)


    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Mouse path for the collapse-fold toggle. The keyboard equivalent is the ``enter`` binding,
        # which dispatches to ``action_toggle_collapsed`` when ``check_action`` says we're in DONE.
        # We don't gate by state here because the button itself is hidden outside DONE — clicking it
        # is only physically possible when the binding would also be active.
        if event.button.id == "cp-collapse":
            event.stop()
            self._vm.toggle_collapsed()
            self.focus()


    def on_edit_instructions_submit(self, event: EditInstructionsSubmit) -> None:
        # Pressing ``enter`` inside the edit-instructions area submits the proposal — same effect as
        # picking "Approve" from the choices list. ``_EditInstructions._on_key`` intercepts enter
        # and posts ``EditInstructionsSubmit`` to get us here; ctrl+enter (sent as ``ctrl+j``) still
        # inserts a literal newline so multi-line instructions are possible. No state guard needed
        # here — the edit-instructions area is hidden outside EDITING, so this handler is unreachable
        # in DONE; the VM's own ``accept`` assert would catch a hypothetical violation.
        self._vm.accept()


    def _maybe_resolve(self) -> None:
        """Called on every VM ``dirty`` emit. The first time we observe the VM reach its DONE state,
        resolve the future with the result. Both accept and cancel route here — ``_build_result`` keys
        on ``completed`` so the caller can distinguish them. Subsequent emits after DONE (e.g. navigation
        through the entries while the widget is still mounted) are no-ops because ``_future`` is done."""
        if self._future.done():
            return
        if self._vm.state != CommitProposalViewModel.State.DONE:
            return

        # Resolve while keeping the widget interactive (the user can still scroll through entries
        # post-resolve, just not mutate them).
        self.resolve(self._build_result(), deactivate_navigation=False)


    def _build_result(self) -> dict[str, Any]:
        return {
            "completed": not self._vm.cancelled,
            "entries": [
                {
                    "title": e.title,
                    "content": e.content,
                    "entry_type": e.entry_type.value,
                    "topic_id": e.topic_id,
                    "excluded": i in self._vm.excluded,
                }
                for i, e in enumerate(self._vm.entries)
            ],
            "edit_instructions": self._vm.edit_instructions or None,
        }


    # ========================================================================================================================
    # Rendering
    # ========================================================================================================================

    def _refresh(self) -> None:
        self._refresh_collapse_button()
        self._refresh_header()
        self._refresh_hints()
        self._refresh_entry_list()
        self._refresh_detail()
        self._refresh_choices()
        self._refresh_edit_instructions()
        self._refresh_resolution()


    def _body_hidden(self) -> bool:
        """Convenience predicate: the "body" elements (header, entry list, detail) collapse together
        in DONE when ``vm.collapsed`` is True. In EDITING and in DONE-expanded, all three are shown.
        """
        return (
            self._vm.state == CommitProposalViewModel.State.DONE
            and self._vm.collapsed
        )


    def _refresh_collapse_button(self) -> None:
        # The button is only meaningful in DONE; the binding is gated by ``check_action`` and the
        # button widget itself is hidden via CSS class outside DONE. Label flips between ▶ (collapsed)
        # and ▼ (expanded) so the affordance reads as "click here to flip whichever way".
        btn = self.query_one("#cp-collapse", Button)
        in_done = self._vm.state == CommitProposalViewModel.State.DONE
        btn.set_class(in_done, "-visible")
        if in_done:
            btn.label = "▶" if self._vm.collapsed else "▼"


    def _refresh_hints(self) -> None:
        # Hints describe editing-only key bindings ("d: exclude/include", etc.); none apply outside
        # EDITING, so hide the entire line in DONE.
        hints = self.query_one("#cp-hints", Static)
        hints.display = self._vm.state == CommitProposalViewModel.State.EDITING


    def _refresh_header(self) -> None:
        # Header text reads as either "Commit Proposal (1 entry)" or "Commit Proposals ({n} entries)".
        # In DONE-collapsed the entry count is subsumed by the resolution summary below, so we hide
        # the header entirely and let the summary stand on its own.
        target = self.query_one("#cp-header", Static)
        if self._body_hidden():
            target.display = False
            return
        target.display = True

        text = Text()
        text.append("  Commit Proposal", style=f"bold {_RED}")

        n = len(self._vm.entries)
        suffix = "y" if n == 1 else "ies"
        text.append(f"  ({n} entr{suffix})", style=_DIM)

        target.update(text)


    def _refresh_entry_list(self) -> None:
        # Hidden in DONE-collapsed (paired with the detail panel via ``_body_hidden``); always visible
        # in EDITING and DONE-expanded.
        scroll = self.query_one("#cp-entry-list-scroll", VerticalScroll)
        if self._body_hidden():
            scroll.display = False
            return
        scroll.display = True

        entries = self._vm.entries
        target = self.query_one("#cp-entry-list", Static)

        if not entries:
            target.update(Text("(no entries)", style=_DIM))
            return

        right_parts: list[str] = []
        stale_flags: list[bool] = []
        for entry in entries:
            label, is_stale = self._format_topic(entry.topic_id)
            right_parts.append(f"{entry.entry_type.value} │ {label}")
            stale_flags.append(is_stale)
        max_right = max(len(r) for r in right_parts)
        max_title = max(len(e.title) for e in entries)
        num_width = len(str(len(entries))) + 2  # "N." plus trailing space

        cursor = self._vm.cursor

        rows: list[Text] = []
        for i, entry in enumerate(entries):
            selected = cursor == i
            excluded = self._vm.is_excluded(i)
            marker_st, body_st, right_st = self._row_styles(
                selected=selected, excluded=excluded, stale=stale_flags[i]
            )

            marker = "► " if selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            gap = " " * (max_title - len(entry.title) + 2)
            right = right_parts[i].rjust(max_right)

            row = Text()
            row.append(marker, style=marker_st)
            row.append(num, style=body_st)
            row.append(entry.title, style=body_st)
            row.append(gap)
            row.append(right, style=right_st)
            rows.append(row)

        target.update(Text("\n").join(rows))
        
        if cursor is not None:
            self._scroll_entry_into_view(cursor)


    def _scroll_entry_into_view(self, row: int) -> None:
        scroll = self.query_one("#cp-entry-list-scroll", VerticalScroll)
        visible = scroll.size.height or 10
        y = scroll.scroll_offset.y
        if row < y:
            scroll.scroll_to(y=row, animate=False)
        elif row >= y + visible:
            scroll.scroll_to(y=row - visible + 1, animate=False)


    @staticmethod
    def _row_styles(*, selected: bool, excluded: bool, stale: bool) -> tuple[str, str, str]:
        """Returns ``(marker_style, body_style, right_style)``. Excluded takes precedence for body/right
        (strike-through, dim); the marker still paints green on selection so the cursor stays visible."""

        marker = f"bold {_FOCUS_GREEN}" if selected else ""
        if excluded:
            return (marker, f"{_EXCLUDED} strike", f"{_EXCLUDED} strike")
        
        right = _RED if stale else _DIM
        if selected:
            return (marker, f"bold {_FOCUS_GREEN}", right)
        
        return ("", "", right)


    def _format_topic(self, topic_id: int | None) -> tuple[str, bool]:
        if topic_id is None:
            return ("(none)", False)
        
        name = self._vm.topic_map.get(topic_id)
        if name is None:
            return (f"(deleted) [{topic_id}]", True)
        
        return (f"{name} [{topic_id}]", False)


    def _refresh_detail(self) -> None:
        # Paired with ``_refresh_entry_list`` via ``_body_hidden``: in DONE-collapsed the entry-list
        # and its detail panel collapse together. Always visible in EDITING and DONE-expanded.
        detail = self.query_one("#cp-detail", Vertical)
        if self._body_hidden():
            detail.display = False
            return
        detail.display = True

        title_input = self.query_one("#cp-detail-title", Input)
        content = self.query_one("#cp-detail-content", TextArea)
        meta = self.query_one("#cp-detail-meta", Static)

        cur = self._vm.cursor
        if cur is None:
            title_input.value = ""
            content.text = ""
            meta.update("")
            return

        entry = self._vm.entries[cur]
        topic_name = self._vm.topic_name(entry.topic_id) or "(none)"
        meta.update(Text.assemble(
            (f"type: {entry.entry_type.value}", _HINT),
            "    ",
            (f"topic: {topic_name}", _HINT),
        ))

        if title_input.value != entry.title:
            title_input.value = entry.title
        if content.text != entry.content:
            content.text = entry.content


    def _current_choices(self) -> list[str]:
        edit_label = (
            _CHOICE_DISMISS_EDITS
            if self._vm.edit_instructions_visible
            else _CHOICE_REQUEST_EDITS
        )
        return [_CHOICE_APPROVE, edit_label, _CHOICE_RESET, _CHOICE_CANCEL]


    def _refresh_choices(self) -> None:
        widget = self.query_one("#cp-choices", _ChoicesList)

        # In DONE the choices list is meaningless (every action it offers — Approve / Edit / Reset /
        # Cancel — is gated by ``check_action``) so we hide it via the ``.-hidden`` CSS class. Skip
        # the rest of the refresh; nothing will be visible regardless. ``display: none`` also drops
        # the widget from focus traversal, so navigation can't land on it from any direction.
        if self._vm.state != CommitProposalViewModel.State.EDITING:
            widget.add_class("-hidden")
            return

        choices = self._current_choices()
        if widget.cursor is not None:
            widget.cursor = max(0, min(widget.cursor, len(choices) - 1))

        # "► Label (hint)" — descriptions are right-aligned by padding the widest prefix to a common width.
        prefix_lengths = [
            2 + len(_CHOICE_LABELS[c]) + 1 + len(f"({_CHOICE_HINTS[c]})")
            for c in choices
        ]
        max_prefix = max(prefix_lengths)

        rows: list[Text] = []
        for i, choice in enumerate(choices):
            selected = i == widget.cursor
            label = _CHOICE_LABELS[choice]
            hint = _CHOICE_HINTS[choice]
            desc = _CHOICE_DESCRIPTIONS[choice]

            row = Text()
            if selected:
                row.append(f"► {label}", style=f"bold {_RED}")
            else:
                row.append(f"  {label}", style=_DIM)
            row.append(f" ({hint})", style=_HINT)
            padding = max_prefix - prefix_lengths[i] + 2
            row.append(" " * padding + desc, style=_HINT)
            rows.append(row)
        widget.update(Text("\n").join(rows))


    def _refresh_edit_instructions(self) -> None:
        # The editable edit-instructions area is only meaningful in EDITING. In DONE we always hide it
        # — even when the user had it open at the time of accept/cancel — and let ``_refresh_resolution``
        # decide whether to show the read-only copy of the submitted text below the resolution status.
        area = self.query_one("#cp-edit-instructions", _EditInstructions)
        visible = (
            self._vm.state == CommitProposalViewModel.State.EDITING
            and self._vm.edit_instructions_visible
        )
        area.set_class(visible, "-visible")
        if not visible:
            return
        if area.text != self._vm.edit_instructions:
            area.text = self._vm.edit_instructions


    def _refresh_resolution(self) -> None:
        # Counterpart to the choices / edit-instructions blocks: visible only in DONE. We render two
        # adjacent elements:
        #
        #   1. ``#cp-resolution`` — a one-line status, rendered in green for non-cancelled outcomes
        #      and red for cancellation. Wording depends on cancel/edit/collapsed:
        #        Expanded (left-aligned, indented):
        #          - "Cancelled"               (cancelled)
        #          - "Accepted"                (accepted, no edit-instructions written)
        #          - "Accepted with edits:"    (accepted + non-empty edits; trailing colon signals
        #                                       that the panel below carries the body)
        #        Collapsed (centered; entry list is hidden so the line grows a count suffix so the
        #        reader can still gauge what they resolved):
        #          - "Cancelled"
        #          - "Accepted — N entries"
        #          - "Accepted — N entries, M excluded"
        #          - "Accepted with edits — N entries[, M excluded]"
        #                                       (edits panel still renders below — see (2))
        #
        #   2. ``#cp-resolution-edits`` — read-only panel carrying the user's submitted edit-instructions
        #      text. Shown whenever accepted-with-edits (both expanded and collapsed); only cancellation
        #      hides it, since the agent won't act on those edits anyway.
        #
        # Both elements default to ``display: none`` in CSS; we toggle ``.-visible`` here.
        status = self.query_one("#cp-resolution", Static)
        edits_panel = self.query_one("#cp-resolution-edits", Static)

        if self._vm.state != CommitProposalViewModel.State.DONE:
            status.set_class(False, "-visible")
            edits_panel.set_class(False, "-visible")
            return

        cancelled = self._vm.cancelled
        has_edits = bool(self._vm.edit_instructions.strip())
        collapsed = self._vm.collapsed
        # Edits panel rides with the resolution status whenever there's something to show, regardless
        # of collapsed/expanded — in the collapsed summary it sits directly below the centered status
        # line so the reader sees both the verdict and the instructions at a glance.
        show_edits_panel = (not cancelled) and has_edits

        # Expanded mode keeps a 2-char indent so the line aligns with the entry-list padding above it.
        # Collapsed mode centers the line via the ``.-centered`` CSS class, so we drop the indent —
        # otherwise those leading spaces would shift the centered text off-axis.
        indent = "" if collapsed else "  "

        # Whole-line color: green for any non-cancelled resolution, red for cancellation. Matches
        # FlashcardReview's ``Session complete`` / ``Session cancelled`` styling so the two widgets
        # read as a family. No bold / no marker glyph — the color alone carries the sentiment.
        if cancelled:
            body = "Cancelled"
            color = _CANCEL_RED
        else:
            color = _DONE_GREEN
            # Expanded with edits gets a trailing colon (panel follows); collapsed-with-edits drops
            # the colon since nothing renders below it.
            if has_edits and not collapsed:
                body = "Accepted with edits:"
            elif has_edits:
                body = "Accepted with edits"
            else:
                body = "Accepted"

            # Count suffix only in the collapsed summary view — the entry list isn't visible so the
            # reader needs the totals here instead.
            if collapsed:
                kept = len(self._vm.entries) - len(self._vm.excluded)
                body += f" — {kept} entr{'y' if kept == 1 else 'ies'}"
                if self._vm.excluded:
                    body += f", {len(self._vm.excluded)} excluded"

        status.update(Text(f"{indent}{body}", style=color))
        status.set_class(True, "-visible")
        status.set_class(collapsed, "-centered")

        if show_edits_panel:
            edits_panel.update(self._vm.edit_instructions)
            edits_panel.set_class(True, "-visible")
        else:
            edits_panel.set_class(False, "-visible")


    # ========================================================================================================================
    # Bindings
    # ========================================================================================================================

    def action_cursor_up(self) -> None:
        # All cross-region navigation lives in action_cursor_up/action_cursor_down. Child widgets let their
        # unused keys bubble up to the outer widget's bindings; these actions inspect ``self.app.focused``
        # and decide what to do based on which region is active.

        # DONE short-circuit: the choices region is hidden and the edit-instructions area is read-only,
        # so navigation is restricted to walking the entry list. We bypass the focus dispatch entirely
        # — even if focus happened to land on one of the now-hidden regions during the EDITING→DONE
        # transition, pressing up should still scroll the entry cursor, never step into a hidden region.
        if self._vm.state != CommitProposalViewModel.State.EDITING:
            self._vm.prev_entry()
            return

        focused = self.app.focused
        choices = self.query_one("#cp-choices", _ChoicesList)
        edits = self.query_one("#cp-edit-instructions", _EditInstructions)

        # Outer (entry list) focused: walk the entry cursor up.
        if focused is self:
            self._vm.prev_entry()

        # Choices focused: walk cursor up, or step out to the entry list past the top.
        elif focused is choices:
            if choices.cursor > 0:
                choices.cursor -= 1
            else:
                choices.cursor = None
                self.focus()

            self._refresh_choices()

        # Edits focused: we only witness this when the cursor is at row 0 in the edit-instructions area;
        # otherwise the keystroke is captured by _EditInstructions._on_key and never bubbles. Thus it's
        # safe to always assume this input means "step up to choices".
        elif focused is edits:
            choices.cursor = len(self._current_choices()) - 1
            choices.focus()
            self._refresh_choices()

    def action_cursor_down(self) -> None:
        # See action_cursor_up. In DONE the choices region is hidden — the "step out the bottom"
        # affordance disappears, so the last entry becomes a hard boundary. We ignore next_entry's
        # return value here since there's nowhere for the fall-through to land.
        if self._vm.state != CommitProposalViewModel.State.EDITING:
            self._vm.next_entry()
            return

        focused = self.app.focused
        choices = self.query_one("#cp-choices", _ChoicesList)
        edits = self.query_one("#cp-edit-instructions", _EditInstructions)

        # Outer (entry list) focused: walk the entry cursor down. At the bottom, step out to the choices
        # list — matches the legacy "step out the bottom" affordance. Choices, in turn, can step further
        # down to the edit-instructions area.
        if focused is self:
            if not self._vm.next_entry():
                choices.focus()
                choices.cursor = 0

        # Choices focused: walk cursor down, or step out to the edit-instructions area past the bottom.
        elif focused is choices:
            items = self._current_choices()

            if choices.cursor is None:
                choices.cursor = 0
            elif choices.cursor < len(items) - 1:
                choices.cursor += 1
            elif self._vm.edit_instructions_visible:
                choices.cursor = None
                edits.focus()

            self._refresh_choices()

    def action_escape_chord(self) -> None:
        # Double-tap escape inside the edit-instructions area discards its contents. We only witness escape
        # here at all because _EditInstructions calls prevent_default on it so it bubbles up; for any other
        # focus this action is a no-op.
        edits = self.query_one("#cp-edit-instructions", _EditInstructions)
        if self.app.focused is not edits:
            return

        now = time.monotonic()

        if (
            self._last_escape_at is not None and
            now - self._last_escape_at < _DOUBLE_ESC_WINDOW
        ):
            self._vm.discard_edit_instructions()
            self._last_escape_at = None

        else:
            self._last_escape_at = now

    def action_cycle_type(self, direction: str) -> None:
        self._vm.cycle_current_entry_type(forward=(direction == "forward"))

    def action_toggle_exclude(self) -> None:
        self._vm.toggle_exclude_current_entry()

    def action_accept(self) -> None:
        self._vm.accept()

    def action_reset(self) -> None:
        self._vm.reset()

    def action_toggle_edit_instructions(self) -> None:
        self._vm.toggle_edit_instructions_area()

        edits = self.query_one("#cp-edit-instructions", _EditInstructions)
        choices = self.query_one("#cp-choices", _ChoicesList)

        if self._vm.edit_instructions_visible:
            edits.focus()
            choices.cursor = None
        elif choices.cursor is not None:
            choices.focus()
        else:
            self.focus()
        
        self._refresh_choices()

    def action_swap_edit_focus(self) -> None:
        # Direct entry-list ↔ edit-instructions hop, skipping choices. If the
        # area isn't open yet, open it and land there.
        edits = self.query_one("#cp-edit-instructions", _EditInstructions)
        if self.app.focused is edits:
            self.focus()
            return
        if not self._vm.edit_instructions_visible:
            self._vm.toggle_edit_instructions_area()
        edits.focus()

    def action_cycle_field(self, direction: str) -> None:
        focused = self.app.focused
        focused_id = focused.id if focused is not None else None
        if focused is self or focused_id not in self._CYCLE_TARGETS:
            current = "outer"
        else:
            current = focused_id

        step = 1 if direction == "forward" else -1
        nxt = self._CYCLE_TARGETS[
            (self._CYCLE_TARGETS.index(current) + step) % len(self._CYCLE_TARGETS)
        ]
        target = self if nxt == "outer" else self.query_one(f"#{nxt}")
        target.focus()

    def action_pick_topic(self, scope: str) -> None:
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        cur = self._vm.cursor
        if scope == "current" and cur is None:
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result is not None:
                topic_id, topic_name = result
                self._vm.topic_map[topic_id] = topic_name
                if scope == "all":
                    self._vm.set_topic_all(topic_id)
                else:
                    assert cur is not None
                    self._vm.set_entry_topic(cur, topic_id)
            self.focus()

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._session_factory),
            callback=on_dismiss,
        )

    def action_select_choice(self) -> None:
        focused = self.app.focused
        choices = self.query_one("#cp-choices", _ChoicesList)

        if focused is not choices:
            return

        current_choice = choices.cursor
        action = [
            self._vm.accept,
            self.action_toggle_edit_instructions,
            self._vm.reset,
            self._vm.cancel
        ][current_choice]

        action()

    def action_toggle_collapsed(self) -> None:
        # Bound on ``enter`` alongside ``action_select_choice``; ``check_action`` ensures only one of
        # the two fires per keypress (DONE → here, EDITING → select_choice). The VM's
        # ``toggle_collapsed`` asserts state == DONE, so we'd crash if both this guard and the binding
        # gate ever drifted out of sync.
        self._vm.toggle_collapsed()

    def action_cancel_interrupt(self) -> None:
        self._vm.cancel()

