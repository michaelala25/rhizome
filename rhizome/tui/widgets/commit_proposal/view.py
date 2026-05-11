"""CommitProposal — thin Textual view over CommitProposalViewModel.

The view is a dumb mirror of the VM: every key event is forwarded to
``vm.on_key``; every dirty emit triggers a single ``_refresh`` that reads
the whole VM and reconciles the widget tree to it. State transitions,
focus management, and choice activation live in the VM.

Editor child widgets (`_TitleInput`, `_ContentInput`, `_EditInstructions`)
forward their committed text up to the VM via ``set_title`` / ``set_content``
on the entry-list sub-VM, and let app-level keys (alt+, ctrl+) bubble so the
VM's global shortcuts and field-cycling still fire while the editor is
focused.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from rhizome.tui.types import DatabaseCommitted

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Input, Static, TextArea

from ..entry_list import ENTRY_ACCENT, ENTRY_DIM, ENTRY_HINT
from ..interrupt import InterruptWidgetBase
from .view_model import (
    Action,
    CommitProposalViewModel,
    EntryListVM,
    KEYBINDINGS,
    KnowledgeEntryType,
)


_RED = ENTRY_ACCENT
_DIM = ENTRY_DIM
_HINT = ENTRY_HINT
_EXCLUDED_DIM = "rgb(60,60,60)"
_FOCUS_GREEN = "rgb(100,200,100)"

_CHOICE_LABELS: dict[str, str] = {
    "accept": "Approve",
    "request_edits": "Edit",
    "reset_all": "Reset",
    "dismiss_edits": "Dismiss edits",
    "cancel": "Cancel",
}
_CHOICE_HINTS: dict[str, str] = {
    "accept": KEYBINDINGS[Action.ACCEPT_ALL],
    "request_edits": KEYBINDINGS[Action.TOGGLE_EDIT_INSTRUCTIONS],
    "reset_all": KEYBINDINGS[Action.RESET_ALL],
    "dismiss_edits": KEYBINDINGS[Action.TOGGLE_EDIT_INSTRUCTIONS],
    "cancel": KEYBINDINGS[Action.CANCEL],
}
_CHOICE_DESCRIPTIONS: dict[str, str] = {
    "accept": "accept the proposal (including any changes made above)",
    "request_edits": "describe the changes you'd like to make",
    "reset_all": "discard all changes and restore the original proposal",
    "dismiss_edits": "hide the edit-instructions area without discarding it",
    "cancel": "cancel the proposal",
}


def _bubble_app_keys(event: events.Key) -> bool:
    """Let app-level keys (alt+*, ctrl+*) bubble up to the parent's on_key
    handler instead of being consumed by the focused editor. Returns True iff
    the event was bubbled (and the caller should stop processing).

    Mirrors the pattern in flashcard_review's ``_AnswerInput`` — the editor
    eats normal typing, but anything that looks like an app shortcut needs to
    reach the VM (cycle field with alt+left/right; global ctrl+a/r/c/e).
    """
    if event.key.startswith("alt+") or event.key.startswith("ctrl+"):
        event.prevent_default()
        return True
    return False


class _TitleInput(Input):
    """Single-line title editor. Forwards changes to the VM and lets
    app-level keys bubble."""

    def _on_key(self, event: events.Key) -> None:
        if _bubble_app_keys(event):
            return
        super()._on_key(event)


class _ContentInput(TextArea):
    """Multiline content editor. Forwards changes to the VM and lets
    app-level keys bubble."""

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if _bubble_app_keys(event):
            return
        super()._on_key(event)


class _EditInstructions(TextArea):
    """Edit-instructions buffer. Lets app-level keys bubble (so ctrl+e to
    dismiss and esc+esc to discard reach the VM); ctrl+j inserts a literal
    newline (Enter would otherwise be ambiguous)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if _bubble_app_keys(event):
            return
        if event.key == "escape":
            # Let the VM see escape so its double-tap discard chord fires.
            event.prevent_default()
            return
        if event.key == "ctrl+j":
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return
        super()._on_key(event)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        # When the VM doesn't own this region, ``can_focus`` is False so
        # clicks don't actually shift focus here — but TextArea's default
        # MouseDown still moves the cursor and restarts the blink, leaving
        # a confusing blinking cursor in an unfocused widget. Swallow the
        # event in that case.
        if not self.has_focus:
            event.stop()
            event.prevent_default()
            return
        await super()._on_mouse_down(event)


class CommitProposal(InterruptWidgetBase):

    DISABLE_CHILDREN_ON_DEACTIVATE = False

    DEFAULT_CSS = """
    CommitProposal {
        height: auto;
        layout: vertical;
        padding: 1 2;
        margin: 1 0;
    }
    CommitProposal #cp-header {
        margin-bottom: 0;
    }
    CommitProposal #cp-hints {
        color: rgb(80,80,80);
        margin-bottom: 1;
    }
    CommitProposal #cp-shared-topic {
        height: 1;
        margin-bottom: 1;
    }
    CommitProposal #cp-entry-list {
        height: auto;
    }
    CommitProposal #cp-detail-panel {
        border: solid $surface-lighten-2;
        margin: 1 0;
        padding: 1 2 1 2;
        height: auto;
    }
    CommitProposal #cp-detail-title {
        background: transparent;
        border: none;
        height: 1;
        padding: 0;
        margin: 0;
    }
    CommitProposal #cp-detail-title:focus {
        border: solid $accent;
        height: 3;
    }
    CommitProposal #cp-detail-meta {
        color: rgb(100,100,100);
        margin: 0 0 1 0;
        padding: 0;
    }
    CommitProposal #cp-detail-content {
        background: transparent;
        border: none;
        height: auto;
        max-height: 12;
        min-height: 3;
        margin: 0;
        padding: 0 1;
    }
    CommitProposal #cp-detail-content:focus {
        border: solid $accent;
    }
    CommitProposal #cp-choices {
        margin-top: 1;
    }
    CommitProposal #cp-edit-instructions {
        background: transparent;
        border: solid $surface-lighten-2;
        margin: 1 0 0 0;
        height: auto;
        min-height: 3;
        max-height: 8;
        padding: 0 1;
        border-title-align: right;
        border-title-color: rgb(80,80,80);
    }
    CommitProposal #cp-edit-instructions:focus {
        border: solid rgb(120,120,140);
    }
    """

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
        super().__init__(**kwargs)
        self._session_factory = session_factory
        self._vm = CommitProposalViewModel(
            entries=entries,
            topic_map=topic_map,
            session_factory=session_factory,
        )
        # Set while ``_refresh`` programmatically rewrites the editors so
        # the change handlers don't echo back into the VM as user edits.
        self._suppress_text_change = False

    @property
    def _topic_map(self) -> dict[int, str]:
        # The data model is the source of truth (it gets mutated when the
        # user picks a new topic via ``apply_topic_selection``).
        return self._vm._data.topic_map

    # ------------------------------------------------------------------
    # Compose & mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="cp-header")
        yield Static(id="cp-hints")
        yield Static(id="cp-shared-topic")
        yield Static(id="cp-entry-list")
        with Vertical(id="cp-detail-panel"):
            yield _TitleInput(id="cp-detail-title")
            yield Static(id="cp-detail-meta")
            yield _ContentInput(id="cp-detail-content")
        yield Static(id="cp-choices")
        yield _EditInstructions(id="cp-edit-instructions")

    def on_mount(self) -> None:
        super().on_mount()

        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.subscribe(self._vm.topic_selection_requests, self._open_topic_selector)
        self._vm.subscribe(self._vm.completion_blocked, self._on_completion_blocked)

        edit_inst = self.query_one("#cp-edit-instructions", _EditInstructions)
        edit_inst.border_title = (
            f"{KEYBINDINGS[Action.TOGGLE_EDIT_INSTRUCTIONS]} to toggle  ·  "
            f"{KEYBINDINGS[Action.DISCARD_EDIT_INSTRUCTIONS]} twice to discard"
        )

        self.query_one("#cp-detail-content", _ContentInput).cursor_blink = False

        self._refresh()
        self.focus()

    # ------------------------------------------------------------------
    # Event forwarding
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        self._vm.on_key(event)

    def on_focus(self, event: events.Focus) -> None:
        """Externally-triggered focus returns (e.g. ctrl+up navigation back
        from chat input) land on the parent widget itself, but the VM's
        logical focus may still point at a child editor. Reconcile here —
        ``_refresh_focus`` already encodes the VM→Textual-focus mapping,
        and its "must own focus somewhere in our subtree" guard is satisfied
        because we just received focus."""
        self._refresh_focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "cp-detail-title" or self._suppress_text_change:
            return
        self._vm.entry_list.set_title(event.value)

    # ------------------------------------------------------------------
    # Topic selection — VM emits a request, we push the modal screen and
    # forward the result back via ``apply_topic_selection``. The VM never
    # sees ``self.app`` or knows the screen exists.
    # ------------------------------------------------------------------

    def _open_topic_selector(
        self,
        request: CommitProposalViewModel.TopicSelectionRequest,
    ) -> None:
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        def on_dismiss(result: tuple[int, str] | None) -> None:
            self._vm.apply_topic_selection(request, result)
            self.focus()

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._session_factory),
            callback=on_dismiss,
        )

    # ------------------------------------------------------------------
    # DB-driven refresh
    # ------------------------------------------------------------------

    async def notify_database_committed(self, event: DatabaseCommitted) -> None:
        """Routed here by the parent (chat_pane). Refresh topics if a topic
        row may have changed; empty ``changed_tables`` means "unknown" so we
        refresh defensively."""
        if not event.changed_tables or "topic" in event.changed_tables:
            await self._vm.refresh_topics()

    def _on_completion_blocked(self) -> None:
        """The VM refused to accept because at least one entry references a
        deleted topic. Refresh topics in case the snapshot was just stale; if
        the entry is *still* stale after refresh, the next accept attempt
        will block again — at which point the View should surface the offending
        rows to the user. (Visual flagging is left to ``_refresh``.)"""
        async def _refresh_then_retry() -> None:
            await self._vm.refresh_topics()
            if not self._vm._data.stale_topic_entry_indices():
                self._vm._accept_all()
        self.run_worker(_refresh_then_retry(), exclusive=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._suppress_text_change:
            return
        if event.text_area.id == "cp-detail-content":
            self._vm.entry_list.set_content(event.text_area.text)
        elif event.text_area.id == "cp-edit-instructions":
            # Edit-instructions buffer is owned by the sub-VM; mirror raw text.
            self._vm.edit_instructions.buffer = event.text_area.text

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        # Painted top-to-bottom; each helper owns exactly one region of the
        # widget. _refresh_focus runs last because earlier helpers toggle
        # ``can_focus`` on child editors that focus reconciliation depends on.
        self._refresh_header()             # "Commit Proposal (N entries)"
        self._refresh_hints()               # one-line keybinding cheatsheet
        self._refresh_shared_topic()        # "Topic (all): …" row
        self._refresh_entry_list()          # numbered list of entry rows
        self._refresh_detail()              # title + meta + content for the cursor entry
        self._refresh_choices()             # accept / edit / reset / cancel column
        self._refresh_edit_instructions()   # multiline buffer (when visible)
        self._refresh_focus()               # route Textual focus to the VM's focused region

    # ----- header / hints ---------------------------------------------

    def _refresh_header(self) -> None:
        # Title + entry count, e.g. "Commit Proposal  (3 entries)".
        text = Text()
        text.append("  Commit Proposal", style=f"bold {_RED}")

        n = len(self._vm._data.entries)
        suffix = "y" if n == 1 else "ies"
        text.append(f"  ({n} entr{suffix})", style=_DIM)
        
        self.query_one("#cp-header", Static).update(text)

    def _refresh_hints(self) -> None:
        # One-line cheatsheet of entry-list keybindings, sourced from KEYBINDINGS
        # so renames in the VM propagate automatically.
        kb = KEYBINDINGS
        hints = (
            f"  {kb[Action.TOGGLE_EXCLUDED]}: exclude/include  "
            f"{kb[Action.CYCLE_TYPE]}: cycle type  "
            f"{kb[Action.OPEN_TOPIC_SELECTOR]}: change topic  "
            f"{kb[Action.OPEN_SHARED_TOPIC_SELECTOR]}: change all topics  "
            f"{kb[Action.CYCLE_FIELD_FORWARD]}: edit fields"
        )
        self.query_one("#cp-hints", Static).update(Text(hints, style=_HINT))

    # ----- shared topic row -------------------------------------------

    def _refresh_shared_topic(self) -> None:
        # Single-line row above the entry list. Shows the topic shared by all
        # entries, or "(mixed)" when entries disagree. Highlights when focused.
        focused = self._vm.focused == CommitProposalViewModel.Region.SHARED_TOPIC
        topic_id = self._common_topic_id()
        if topic_id is None:
            label, is_stale = "(mixed)", False
        else:
            label, is_stale = self._format_topic(topic_id)

        marker_style = f"bold {_FOCUS_GREEN}" if focused else ""
        if is_stale:
            body_style = f"bold {_RED}" if focused else _RED
        else:
            body_style = f"bold {_FOCUS_GREEN}" if focused else _DIM
        text = Text()
        text.append("► " if focused else "  ", style=marker_style)
        text.append(f"Topic (all): {label}", style=body_style)
        self.query_one("#cp-shared-topic", Static).update(text)

    def _common_topic_id(self) -> int | None:
        ids = {e.topic_id for e in self._vm._data.entries}
        return ids.pop() if len(ids) == 1 else None

    def _format_topic(self, topic_id: int | None) -> tuple[str, bool]:
        """Render a topic_id for display. Returns ``(label, is_stale)`` where
        ``is_stale`` is True iff the id is set but missing from the topic map
        — i.e. the topic was deleted and the entry can't be safely committed
        until the user picks a new topic."""
        if topic_id is None:
            return ("(none)", False)
        name = self._topic_map.get(topic_id)
        if name is None:
            return (f"(deleted) [{topic_id}]", True)
        return (f"{name} [{topic_id}]", False)

    # ----- entry list -------------------------------------------------

    def _refresh_entry_list(self) -> None:
        # Renders one row per entry, laid out as:
        #   "► 1.  <title>          <type> │ <topic> [id]"
        # The title column flexes to the longest title; the right column
        # (type + topic) is right-padded to a uniform width so it lines up.
        entries = self._vm._data.entries
        focused = self._vm.focused == CommitProposalViewModel.Region.ENTRY_LIST
        cursor = self._vm.entry_list.cursor

        # Pre-compute the right-column strings so we can size the column to
        # the widest one before we start painting rows. ``stale_flags`` tracks
        # which rows reference a deleted topic so the row paints in red.
        right_parts: list[str] = []
        stale_flags: list[bool] = []
        for entry in entries:
            etype = self._format_type(entry.entry_type)
            topic_label, is_stale = self._format_topic(entry.topic_id)
            right_parts.append(f"{etype} │ {topic_label}")
            stale_flags.append(is_stale)
        max_right = max((len(r) for r in right_parts), default=0)

        num_width = len(str(len(entries))) + 2  # "N." + trailing space
        max_title = max((len(e.title) for e in entries), default=0)

        text = Text()
        for i, entry in enumerate(entries):
            if i > 0:
                text.append("\n")
            is_selected = focused and cursor == i
            is_excluded = self._vm._data.is_excluded(i)

            # Row pieces — marker, number, title, gap to right column, right column.
            marker = "► " if is_selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)
            title = entry.title
            right = right_parts[i].rjust(max_right)
            gap = " " * (max_title - len(title) + 2)

            # Excluded entries take precedence over selection (struck-through, dim).
            # Stale-topic entries get a red right column so the user can see at a
            # glance which rows are blocking commit; the title column keeps its
            # normal styling so selection/focus still reads correctly.
            if is_excluded:
                style = f"{_EXCLUDED_DIM} strike"
                right_style = f"{_EXCLUDED_DIM} strike"
            elif is_selected:
                style = f"bold {_FOCUS_GREEN}"
                right_style = _RED if stale_flags[i] else _DIM
            else:
                style = ""
                right_style = _RED if stale_flags[i] else _DIM

            text.append(marker, style=f"bold {_FOCUS_GREEN}" if is_selected else "")
            text.append(num, style=style)
            text.append(title, style=style)
            text.append(gap)
            text.append(right, style=right_style)

        self.query_one("#cp-entry-list", Static).update(text)

    @staticmethod
    def _format_type(value: KnowledgeEntryType | None) -> str:
        return value.name.lower() if value is not None else ""

    # ----- detail panel -----------------------------------------------

    def _refresh_detail(self) -> None:
        # Bordered panel below the entry list showing the current entry's
        # title (Input), meta line (Type / Topic), and content (TextArea).
        # Hidden entirely when the proposal has no entries.
        entries = self._vm._data.entries
        if not entries:
            self.query_one("#cp-detail-panel", Vertical).display = False
            return
        self.query_one("#cp-detail-panel", Vertical).display = True

        idx = max(0, min(self._vm.entry_list.cursor, len(entries) - 1))
        entry = entries[idx]

        panel = self.query_one("#cp-detail-panel", Vertical)
        panel.border_title = f"Entry {idx + 1}"

        # Title editor.
        # ``can_focus`` mirrors VM intent: editors only accept focus when the
        # entry-list field state says they should. This is what enforces
        # unidirectional VM → V focus — a stray mouse click can't focus an
        # editor the VM hasn't transitioned into.
        title_input = self.query_one("#cp-detail-title", _TitleInput)
        title_input.can_focus = (
            self._vm.focused == CommitProposalViewModel.Region.ENTRY_LIST
            and self._vm.entry_list.field == EntryListVM.Field.TITLE
        )
        if title_input.value != entry.title:
            self._suppress_text_change = True
            try:
                title_input.value = entry.title
            finally:
                self._suppress_text_change = False

        # Content editor — same can_focus / suppress-echo dance as the title.
        content_area = self.query_one("#cp-detail-content", _ContentInput)
        content_area.can_focus = (
            self._vm.focused == CommitProposalViewModel.Region.ENTRY_LIST
            and self._vm.entry_list.field == EntryListVM.Field.CONTENT
        )
        if content_area.text != entry.content:
            self._suppress_text_change = True
            try:
                content_area.load_text(entry.content)
            finally:
                self._suppress_text_change = False

        # Meta line (read-only): "Type: …   Topic: … [id]   (excluded)".
        # Stale topic id is rendered in red as a hint that the user must
        # re-pick before the proposal can be accepted.
        etype = self._format_type(entry.entry_type)
        topic_label, is_stale = self._format_topic(entry.topic_id)
        topic_markup = f"[{_RED}]{topic_label}[/]" if is_stale else topic_label
        excluded_note = "  [dim](excluded)[/dim]" if self._vm._data.is_excluded(idx) else ""
        self.query_one("#cp-detail-meta", Static).update(
            f"  Type: {etype}   Topic: {topic_markup}{excluded_note}"
        )

    # ----- choices ----------------------------------------------------

    def _refresh_choices(self) -> None:
        # Renders the bottom action column. Each row is:
        #   "► <Label> (<hint>)        <description>"
        # The hint column is right-padded so descriptions line up. The choice
        # set itself is owned by the VM (it shrinks/grows with the edit-
        # instructions panel), so we just iterate whatever it hands us.
        choices = self._vm.choices.items
        focused = self._vm.focused == CommitProposalViewModel.Region.CHOICES_LIST
        cursor = self._vm.choices.cursor % max(len(choices), 1)

        # Width of "► <Label> (<hint>)" per row, used to align descriptions.
        prefix_lengths = [
            2 + len(_CHOICE_LABELS[c]) + 1 + len(f"({_CHOICE_HINTS[c]})")
            for c in choices
        ]
        max_prefix = max(prefix_lengths, default=0)

        text = Text()
        for i, choice in enumerate(choices):
            if i > 0:
                text.append("\n")
            is_selected = focused and i == cursor
            label = _CHOICE_LABELS[choice]
            hint = _CHOICE_HINTS[choice]
            desc = _CHOICE_DESCRIPTIONS[choice]
            if is_selected:
                text.append(f"► {label}", style=f"bold {_RED}")
            else:
                text.append(f"  {label}", style=_DIM)
            text.append(f" ({hint})", style=_HINT)
            padding = max_prefix - prefix_lengths[i] + 2
            text.append(" " * padding + desc, style=_HINT)
        self.query_one("#cp-choices", Static).update(text)

    # ----- edit instructions ------------------------------------------

    def _refresh_edit_instructions(self) -> None:
        # Multiline buffer below the choices column. Hidden by default; shown
        # only when the user opts in via ctrl+e or the "Edit" choice. We mirror
        # the VM's buffer into the TextArea here whenever the two diverge
        # (i.e. the VM was edited from somewhere other than this widget).
        edit_inst = self.query_one("#cp-edit-instructions", _EditInstructions)
        edit_inst.display = self._vm.edit_instructions.visible
        # Only focusable when the VM says this region owns focus.
        edit_inst.can_focus = (
            self._vm.focused == CommitProposalViewModel.Region.EDIT_INSTRUCTIONS
        )
        if not self._vm.edit_instructions.visible:
            return
        if edit_inst.text != self._vm.edit_instructions.buffer:
            self._suppress_text_change = True
            try:
                edit_inst.load_text(self._vm.edit_instructions.buffer)
            finally:
                self._suppress_text_change = False

    # ----- focus reconciliation ---------------------------------------

    def _refresh_focus(self) -> None:
        """Route Textual focus to match the VM's logical focus.

        VM is the source of truth: every dirty emit reconciles Textual focus
        to whichever widget the VM currently delegates input to. Most regions
        don't take focus themselves — the parent eats keys and forwards to
        ``vm.on_key``. The exceptions are:

          - entry list with ``field`` in {TITLE, CONTENT}: focus the matching
            editor so typing lands there.
          - edit instructions visible AND focused: focus the textarea.

        Only reconciles when we already own focus somewhere in our subtree —
        otherwise we'd steal focus from sibling widgets (chat input, etc.).
        Initial routing on widget mount is handled by ``on_mount`` calling
        ``self.focus()``.
        """
        app_focused = self.app.focused
        own_subtree = {self, *self.query("*")}
        if app_focused not in own_subtree:
            return

        target: Any = self
        in_entry_list = self._vm.focused == CommitProposalViewModel.Region.ENTRY_LIST
        if in_entry_list and self._vm.entry_list.field == EntryListVM.Field.TITLE:
            target = self.query_one("#cp-detail-title", _TitleInput)
        elif in_entry_list and self._vm.entry_list.field == EntryListVM.Field.CONTENT:
            target = self.query_one("#cp-detail-content", _ContentInput)
        elif self._vm.focused == CommitProposalViewModel.Region.EDIT_INSTRUCTIONS:
            target = self.query_one("#cp-edit-instructions", _EditInstructions)

        if app_focused is not target:
            target.focus()
