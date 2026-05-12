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
from textual.widgets import Input, Static, TextArea

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
# route those (jump to choices, double-tap discard).
#
# ------------------------------------------------------------------------------------------------------------------------


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
    ]

    DEFAULT_CSS = """
    CommitProposal {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 1 0;
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self._EDITING_ONLY_ACTIONS:
            return self._vm.state == CommitProposalViewModel.State.EDITING
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
        self._refresh_header()
        self._refresh_entry_list()
        self._refresh_detail()
        self._refresh_choices()
        self._refresh_edit_instructions()


    def _refresh_header(self) -> None:
        # Header text reads as either "Commit Proposal (1 entry)" or "Commit Proposals ({n} entries)"

        text = Text()
        text.append("  Commit Proposal", style=f"bold {_RED}")

        n = len(self._vm.entries)
        suffix = "y" if n == 1 else "ies"
        text.append(f"  ({n} entr{suffix})", style=_DIM)

        self.query_one("#cp-header", Static).update(text)


    def _refresh_entry_list(self) -> None:
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
        area = self.query_one("#cp-edit-instructions", _EditInstructions)
        area.set_class(self._vm.edit_instructions_visible, "-visible")
        if not self._vm.edit_instructions_visible:
            return
        if area.text != self._vm.edit_instructions:
            area.text = self._vm.edit_instructions


    # ========================================================================================================================
    # Bindings
    # ========================================================================================================================

    def action_cursor_up(self) -> None:
        # All cross-region navigation lives in action_cursor_up/action_cursor_down. Child widgets let their
        # unused keys bubble up to the outer widget's bindings; these actions inspect ``self.app.focused``
        # and decide what to do based on which region is active.

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

        if self._vm.edit_instructions_visible:
            self.query_one("#cp-edit-instructions", _EditInstructions).focus()
        else:
            self.focus()

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
            self._vm.toggle_edit_instructions_area,
            self._vm.reset,
            self._vm.cancel
        ][current_choice]

        action()

    def action_cancel_interrupt(self) -> None:
        self._vm.cancel()

