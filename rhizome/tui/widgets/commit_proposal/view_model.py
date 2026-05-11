# User actions:
#   - Browse the list of entries
#   - Modify entry
#       - Modify title
#       - Modify content
#       - Modify type
#       - Modify topic
#   - Write edit instructions
#   - Accept all
#   - Reset all
#   - Reset single entry
#   - Set topic for all
#   - Discard edit instructions
#   - Cancel

# Behaviour:
#   - There's an active "region" and an active "widget"
#   - Regions:
#       - Entry list
#       - Entry title
#       - Entry content
#       - Shared topic
#       - Topic selector
#           - Hidden by default
#       - Choices list
#       - Edit instructions
#           - Hidden by default

# Behaviour by region:
#   Entry list
#       - Navigating up/down - navigates betwen entries
#       - Navigating up from the first entry brings you to the shared topic line
#           - boundary condition - possibly "view" dependent?
#       - Navigating down from the last entry brings you to the first entry of the choices list
#           - boundary condition - possibly "view" dependent?
#       - alt+left - brings you to the entry title
#       - alt+right - brings you to the entry content (alt+left cycles entry list, title, content. alt+right reverse)
#       - t - topic selector
#       - d - toggle striked entry
#       - f - cycle entry type
#       - r - reset entry to original state
#       - shift+t - shared topic selector
#       - [requires edit instructions to be visible] alt+up/down - cycle between edit instructions area and here

#  Entry title
#       - alt+left/right - cycle between entry list/title/content
#       - any other typing - submit to entry title buffer

#  Entry content
#       - alt+left/right - cycle between entry list/title/content
#       - any other typing - submit to entry content buffer

#  Shared topic selector
#       - navigating down from here brings you back to the first item of the entry list
#       - enter - bring up topic selector

#  Topic selector
#       - behaviour deferred to a topic tree
#       - resolves back to the widget when done

#  Choices list
#       - navigating up/down - navigates between choices
#       - navigating up from the first choice brings you to the last entry
#           - boundary condition - possibly "view" dependent?
#       - four choices IF edit instructions not visible:
#           - accept (ctrl+a), request edits (ctrl+e), reset all (ctrl+r), cancel (ctrl+c)
#       - three choices if edit instructions is NOT visible:
#           - dismiss edits

#  Edit instructions
#       - ctrl+e - dismiss edit instructions (without discarding buffer)
#       - navigating up from first character - brings you to last choice in choices list
#           - boundary condition - possibly "view" dependent?
#       - esc+esc (in quick succession) - discard edit instructions

#  From anywhere:
#       - ctrl+a - accept all
#       - ctrl+r - reset all
#       - ctrl+e - request edits (brings up the edit instructions region)
#       - ctrl+e (with edit instruction visible) - close edit instructions (doesn't clear the buffer)
#       - ctrl+c - cancel

import copy
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from textual import events

from rhizome.logs import get_logger
from rhizome.tui.widgets.view_model_base import CallbackGroup, Emitter, ViewModelBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


_logger = get_logger("tui.commit_proposal_vm")


# Max gap (seconds) between two ``escape`` presses to count as a double-tap.
# Matches the chat input's discard-buffer chord (chat_input.py).
_DOUBLE_ESC_WINDOW = 0.5


class KnowledgeEntryType(Enum):
    FACT = auto()
    EXPOSITION = auto()
    OVERVIEW = auto()


class Action(Enum):
    """Semantic keyboard actions handled by the view-model.

    Multiple actions may share a key — the dispatcher disambiguates by focused
    region (e.g. ``enter`` is ACTIVATE_TOPIC_SELECTOR in SHARED_TOPIC and
    ACTIVATE_CHOICE in CHOICES_LIST).
    """
    # Global (work from any region)
    ACCEPT_ALL = auto()                 # ctrl+a
    RESET_ALL = auto()                  # ctrl+r
    CANCEL = auto()                     # ctrl+c
    TOGGLE_EDIT_INSTRUCTIONS = auto()   # ctrl+e

    # Vertical navigation (region-specific semantics)
    MOVE_UP = auto()                    # up
    MOVE_DOWN = auto()                  # down

    # Entry list — row mode
    CYCLE_FIELD_BACKWARD = auto()       # alt+left
    CYCLE_FIELD_FORWARD = auto()        # alt+right
    OPEN_TOPIC_SELECTOR = auto()        # t
    OPEN_SHARED_TOPIC_SELECTOR = auto() # shift+t
    TOGGLE_EXCLUDED = auto()            # d
    CYCLE_TYPE = auto()                 # f
    RESET_ENTRY = auto()                # r

    # Shared topic / choices list
    ACTIVATE = auto()                   # enter (ACTIVATE_TOPIC_SELECTOR or ACTIVATE_CHOICE depending on region)

    # Edit instructions
    DISCARD_EDIT_INSTRUCTIONS = auto()  # esc+esc


KEYBINDINGS: dict[Action, str] = {
    Action.ACCEPT_ALL: "ctrl+a",
    Action.RESET_ALL: "ctrl+r",
    Action.CANCEL: "ctrl+c",
    Action.TOGGLE_EDIT_INSTRUCTIONS: "ctrl+e",
    Action.MOVE_UP: "up",
    Action.MOVE_DOWN: "down",
    Action.CYCLE_FIELD_BACKWARD: "alt+left",
    Action.CYCLE_FIELD_FORWARD: "alt+right",
    Action.OPEN_TOPIC_SELECTOR: "t",
    Action.OPEN_SHARED_TOPIC_SELECTOR: "T",
    Action.TOGGLE_EXCLUDED: "d",
    Action.CYCLE_TYPE: "f",
    Action.RESET_ENTRY: "r",
    Action.ACTIVATE: "enter",
    Action.DISCARD_EDIT_INSTRUCTIONS: "escape",  # double-tap; see _on_key_edit_instructions
}


# ---------- Data model ----------


@dataclass
class CommitProposalEntry:
    title: str
    content: str
    entry_type: KnowledgeEntryType | None
    topic_id: int | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommitProposalEntry":
        etype = data.get("entry_type")
        if isinstance(etype, str):
            etype = KnowledgeEntryType[etype.upper()]
        return cls(
            title=data["title"],
            content=data["content"],
            entry_type=etype,
            topic_id=data.get("topic_id"),
        )


class CommitProposalDataModel:
    def __init__(
        self,
        entries: list[dict[str, Any] | CommitProposalEntry],
        topic_map: dict[int, str],
    ):
        self._initial_entries: list[CommitProposalEntry] = [
            e if isinstance(e, CommitProposalEntry) else CommitProposalEntry.from_dict(e)
            for e in entries
        ]
        self._current_entries: list[CommitProposalEntry] = [
            copy.deepcopy(e) for e in self._initial_entries
        ]
        self._topic_map = dict(topic_map)
        self._excluded: set[int] = set()

    @property
    def entries(self) -> list[CommitProposalEntry]:
        return self._current_entries

    @property
    def topic_map(self) -> dict[int, str]:
        return self._topic_map

    def entry(self, idx: int) -> CommitProposalEntry:
        return self._current_entries[idx]

    def set_entry_field(self, idx: int, field: str, value: Any) -> None:
        setattr(self._current_entries[idx], field, value)

    def set_topic_for_entry(self, idx: int, topic_id: int, topic_name: str) -> None:
        self._topic_map[topic_id] = topic_name
        self._current_entries[idx].topic_id = topic_id

    def set_topic_for_all(self, topic_id: int, topic_name: str) -> None:
        self._topic_map[topic_id] = topic_name
        for entry in self._current_entries:
            entry.topic_id = topic_id

    def reset_entry(self, idx: int) -> None:
        self._current_entries[idx] = copy.deepcopy(self._initial_entries[idx])

    def toggle_excluded(self, idx: int) -> None:
        self._excluded ^= {idx}

    def is_excluded(self, idx: int) -> bool:
        return idx in self._excluded

    def reset_all(self) -> None:
        self._current_entries = [copy.deepcopy(e) for e in self._initial_entries]
        self._excluded = set()

    def replace_topic_map(self, topic_map: dict[int, str]) -> None:
        """Swap in a freshly-fetched topic map (e.g. after a DB commit elsewhere
        renamed/added/deleted a topic). Entries' ``topic_id`` values are left
        untouched — if a referenced topic was deleted, the View renders it as
        stale and ``stale_topic_entry_indices`` will surface it."""
        self._topic_map = dict(topic_map)

    def stale_topic_entry_indices(self) -> list[int]:
        """Indices of non-excluded entries whose ``topic_id`` is set but not
        present in the current ``topic_map`` — i.e. the topic was deleted (or
        never existed) and we can't safely commit them."""
        return [
            i
            for i, entry in enumerate(self._current_entries)
            if i not in self._excluded
            and entry.topic_id is not None
            and entry.topic_id not in self._topic_map
        ]


# ---------- Sub-view-models ----------

class EntryListVM:
    """Owns entry cursor + which field of the current entry is being edited."""

    class Field(Enum):
        NONE = auto()      # cursor is on the row, not editing a field
        TITLE = auto()
        CONTENT = auto()

    def __init__(self, data: CommitProposalDataModel):
        self._data = data
        self.cursor: int = 0
        self.field: EntryListVM.Field = EntryListVM.Field.NONE

    def can_focus(self) -> bool:
        return len(self._data.entries) > 0

    def on_focus_enter(self, from_direction: str) -> None:
        # 'down' means we entered from above (e.g. shared topic), so land on first
        # 'up' means we entered from below (e.g. choices), so land on last
        if from_direction == "down":
            self.cursor = 0
        elif from_direction == "up":
            self.cursor = len(self._data.entries) - 1
        self.field = EntryListVM.Field.NONE

    def move_up(self) -> bool:
        """Returns True if handled, False if we want to leave the region upward."""
        if self.field is not EntryListVM.Field.NONE:
            return True  # field-editing modes swallow vertical nav
        if self.cursor > 0:
            self.cursor -= 1
            return True
        return False

    def move_down(self) -> bool:
        if self.field is not EntryListVM.Field.NONE:
            return True
        if self.cursor < len(self._data.entries) - 1:
            self.cursor += 1
            return True
        return False

    def cycle_field_forward(self) -> None:
        order = [self.Field.NONE, self.Field.TITLE, self.Field.CONTENT]
        self.field = order[(order.index(self.field) + 1) % len(order)]

    def cycle_field_backward(self) -> None:
        order = [self.Field.NONE, self.Field.CONTENT, self.Field.TITLE]
        self.field = order[(order.index(self.field) + 1) % len(order)]

    def toggle_excluded(self) -> None:
        self._data.toggle_excluded(self.cursor)

    def reset_current(self) -> None:
        self._data.reset_entry(self.cursor)

    def cycle_type(self) -> None:
        types = list(KnowledgeEntryType)
        cur = self._data.entry(self.cursor).entry_type
        idx = types.index(cur) if cur in types else -1
        self._data.set_entry_field(
            self.cursor, "entry_type", types[(idx + 1) % len(types)]
        )

    def set_title(self, text: str) -> None:
        """Commit edited title text for the current entry. Called by the view's
        title TextArea on submit/blur — same pattern as Flashcard.set_user_answer."""
        self._data.set_entry_field(self.cursor, "title", text)

    def set_content(self, text: str) -> None:
        """Commit edited content text for the current entry. Called by the view's
        content TextArea on submit/blur."""
        self._data.set_entry_field(self.cursor, "content", text)


class SharedTopicVM:
    def __init__(self, data: CommitProposalDataModel):
        self._data = data

    def can_focus(self) -> bool:
        return True  # always present

    def on_focus_enter(self, from_direction: str) -> None:
        pass


class ChoicesListVM:
    """Owns the visible choice list and its cursor. The parent VM is
    responsible for swapping ``items`` when the relevant context changes
    (e.g. the edit-instructions panel toggles)."""

    def __init__(self, items: list[str]):
        self.items: list[str] = list(items)
        self.cursor: int = 0

    def can_focus(self) -> bool:
        return True

    def on_focus_enter(self, from_direction: str) -> None:
        if from_direction == "down":
            self.cursor = 0
        else:
            self.cursor = max(0, len(self.items) - 1)

    def move_up(self) -> bool:
        if self.cursor > 0:
            self.cursor -= 1
            return True
        return False

    def move_down(self) -> bool:
        if self.cursor < len(self.items) - 1:
            self.cursor += 1
            return True
        return False

    def set_items(self, items: list[str]) -> None:
        """Replace the visible items and re-clamp the cursor."""
        self.items = list(items)
        self.clamp_cursor()

    def clamp_cursor(self) -> None:
        """Re-clamp cursor after ``items`` shrinks (e.g. the edit-instructions
        panel just got dismissed, removing its choice)."""
        n = len(self.items)
        if n == 0:
            self.cursor = 0
        elif self.cursor >= n:
            self.cursor = n - 1


class EditInstructionsVM:
    def __init__(self):
        self.buffer: str = ""
        self.visible: bool = False
        self.cursor: int = 0  # within buffer

    def can_focus(self) -> bool:
        return self.visible

    def on_focus_enter(self, from_direction: str) -> None:
        self.cursor = 0 if from_direction == "down" else len(self.buffer)

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False  # buffer preserved

    def discard(self) -> None:
        """Clear the buffer; leaves the panel visible and focused."""
        self.buffer = ""
        self.cursor = 0


# ---------- Parent VM ----------

class CommitProposalViewModel(ViewModelBase):

    class Region(Enum):
        SHARED_TOPIC = auto()
        ENTRY_LIST = auto()
        CHOICES_LIST = auto()
        EDIT_INSTRUCTIONS = auto()

    class TopicScope(Enum):
        ENTRY = auto()       # change the topic of one entry (idx)
        ALL = auto()         # change the topic of every entry

    class Callbacks(Enum):
        TOPIC_SELECTION_REQUESTS = "topic_selection_requests"
        COMPLETION_BLOCKED = "completion_blocked"

    @dataclass
    class TopicSelectionRequest:
        """Emitted to ``topic_selection_requests`` when the user invokes a
        topic picker. ``entry_idx`` is meaningful only when ``scope`` is
        ``ENTRY``; for ``ALL`` it is None."""
        scope: "CommitProposalViewModel.TopicScope"
        entry_idx: int | None

    def __init__(
        self,
        entries: list[dict[str, Any] | CommitProposalEntry],
        topic_map: dict[int, str],
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ):
        super().__init__()
        self._data = CommitProposalDataModel(entries, topic_map)
        # Live runtime resource — held on the VM, never on the data model.
        # Optional so the VM remains constructible in tests / sample paths
        # that don't have a DB; ``refresh_topics`` no-ops when absent.
        self._session_factory = session_factory

        self.entry_list = EntryListVM(self._data)
        self.shared_topic = SharedTopicVM(self._data)
        self.edit_instructions = EditInstructionsVM()
        # The parent VM owns the choice-list contents; ``_sync_choices`` is
        # the single place that recomputes them from the rest of the VM state.
        self.choices = ChoicesListVM(self._compute_choices())

        # Logical focus order, top to bottom. The view is expected to render
        # in this order; if it doesn't, that's the view's problem.
        self._focus_order = [
            self.Region.SHARED_TOPIC,
            self.Region.ENTRY_LIST,
            self.Region.CHOICES_LIST,
            self.Region.EDIT_INSTRUCTIONS,
        ]
        self._focused = self.Region.ENTRY_LIST

        # Timestamp of the most recent ``escape`` while edit-instructions had
        # focus — used to detect the discard chord (two within _DOUBLE_ESC_WINDOW).
        self._last_escape_at: float = 0.0

        # Observable: emitted as a ``TopicSelectionRequest`` when the user asks to pick a topic. The
        # view subscribes a handler that opens its preferred picker (modal screen, inline overlay, …)
        # and then calls ``apply_topic_selection`` with the result. Keeps screen-pushing out of the VM.
        self.topic_selection_requests = CallbackGroup(
            CommitProposalViewModel.Callbacks.TOPIC_SELECTION_REQUESTS, []
        )

        # Observable: emitted when the user tries to complete (accept / submit edits) but at least one
        # entry references a topic_id missing from ``topic_map`` — i.e. the topic was deleted between
        # the snapshot and now. The view refreshes topics from the DB and re-attempts.
        self.completion_blocked = CallbackGroup(
            CommitProposalViewModel.Callbacks.COMPLETION_BLOCKED, []
        )

    # ----- region lookup -----

    def _vm_for(self, region: "CommitProposalViewModel.Region"):
        return {
            self.Region.SHARED_TOPIC: self.shared_topic,
            self.Region.ENTRY_LIST: self.entry_list,
            self.Region.CHOICES_LIST: self.choices,
            self.Region.EDIT_INSTRUCTIONS: self.edit_instructions,
        }[region]

    # ----- focus management -----

    @property
    def focused(self) -> "CommitProposalViewModel.Region":
        return self._focused

    def _focus_region(
        self, region: "CommitProposalViewModel.Region", from_direction: str,
    ) -> None:
        # Renamed from ``_focus`` because that name now resolves to the inherited ``focus``
        # CallbackGroup attribute (set on the instance in ``ViewModelBase.__init__``).
        self._focused = region
        self._vm_for(region).on_focus_enter(from_direction)
        self.emit(self.dirty)

    def focus_next(self, direction: str) -> None:
        """direction: 'up' or 'down'. Walks _focus_order skipping unfocusable regions."""
        idx = self._focus_order.index(self._focused)
        step = -1 if direction == "up" else 1
        i = idx + step
        while 0 <= i < len(self._focus_order):
            region = self._focus_order[i]
            if self._vm_for(region).can_focus():
                self._focus_region(region, direction)
                return
            i += step
        # No neighbor — stay put.

    def notify_focused(self, emitter: Emitter | None = None) -> None:
        """Reconcile region focus when the widget regains Textual focus from elsewhere
        (e.g. after a topic-selector modal closes).

        While we were unfocused the focusable set may have shifted — entries could have been
        edited away, the edit-instructions panel may have toggled, etc. If the current
        ``_focused`` region is still focusable we preserve it (and its cursor state); otherwise
        we walk ``_focus_order`` and land on the first focusable region we find."""
        if emitter is None:
            emitter = self
        if not self._vm_for(self._focused).can_focus():
            for region in self._focus_order:
                if self._vm_for(region).can_focus():
                    self._focused = region
                    self._vm_for(region).on_focus_enter("down")
                    break
        emitter.emit(self.dirty)

    # ----- input dispatch -----

    def on_key(self, event: events.Key) -> None:
        _logger.info(
            "on_key: focused=%s key=%r field=%s",
            self._focused.name,
            event.key,
            self.entry_list.field.name,
        )
        # Global shortcuts (work from anywhere). Stop bubbling so ancestor
        # widgets (e.g. ChatPane's ctrl+r → refocus_resources) don't also act
        # on the same key.
        if event.key == KEYBINDINGS[Action.ACCEPT_ALL]:
            self._accept_all()
            event.stop()
            event.prevent_default()
            return
        if event.key == KEYBINDINGS[Action.RESET_ALL]:
            self._data.reset_all()
            self.emit(self.dirty)
            event.stop()
            event.prevent_default()
            return
        if event.key == KEYBINDINGS[Action.CANCEL]:
            self._cancel()
            event.stop()
            event.prevent_default()
            return
        if event.key == KEYBINDINGS[Action.TOGGLE_EDIT_INSTRUCTIONS]:
            self._toggle_edit_instructions()
            event.stop()
            event.prevent_default()
            return

        # Per-region dispatch
        match self._focused:
            case self.Region.ENTRY_LIST:
                self._on_key_entry_list(event)
            case self.Region.SHARED_TOPIC:
                self._on_key_shared_topic(event)
            case self.Region.CHOICES_LIST:
                self._on_key_choices(event)
            case self.Region.EDIT_INSTRUCTIONS:
                self._on_key_edit_instructions(event)

    def _on_key_entry_list(self, event: events.Key) -> None:
        # When a field is focused, the view's TextArea owns key handling — its
        # bubbled events never reach us. The only field-mode keys we still see
        # are alt+left/right (the view should let those propagate so we can cycle
        # field focus back to row mode or to the sibling field).
        if self.entry_list.field is not EntryListVM.Field.NONE:
            if event.key == KEYBINDINGS[Action.CYCLE_FIELD_BACKWARD]:
                self.entry_list.cycle_field_backward()
                self.emit(self.dirty)
                return
            if event.key == KEYBINDINGS[Action.CYCLE_FIELD_FORWARD]:
                self.entry_list.cycle_field_forward()
                self.emit(self.dirty)
                return
            return

        # Row mode
        if event.key == KEYBINDINGS[Action.MOVE_UP]:
            if not self.entry_list.move_up():
                self.focus_next("up")
            else:
                self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.MOVE_DOWN]:
            if not self.entry_list.move_down():
                self.focus_next("down")
            else:
                self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.CYCLE_FIELD_BACKWARD]:
            self.entry_list.cycle_field_backward()
            self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.CYCLE_FIELD_FORWARD]:
            self.entry_list.cycle_field_forward()
            self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.OPEN_TOPIC_SELECTOR]:
            self._request_topic_selection(self.TopicScope.ENTRY, self.entry_list.cursor)
            return
        if event.key == KEYBINDINGS[Action.OPEN_SHARED_TOPIC_SELECTOR]:
            self._request_topic_selection(self.TopicScope.ALL, None)
            event.stop()
            event.prevent_default()
            return
        if event.key == KEYBINDINGS[Action.TOGGLE_EXCLUDED]:
            self.entry_list.toggle_excluded()
            self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.CYCLE_TYPE]:
            self.entry_list.cycle_type()
            self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.RESET_ENTRY]:
            self.entry_list.reset_current()
            self.emit(self.dirty)
            return

    def _on_key_shared_topic(self, event: events.Key) -> None:
        if event.key == KEYBINDINGS[Action.MOVE_DOWN]:
            self.focus_next("down")
            return
        if event.key in [
            KEYBINDINGS[Action.ACTIVATE],
            KEYBINDINGS[Action.OPEN_SHARED_TOPIC_SELECTOR]
        ]:
            self._request_topic_selection(self.TopicScope.ALL, None)
            event.stop()
            event.prevent_default()
            return

    def _on_key_choices(self, event: events.Key) -> None:
        if event.key == KEYBINDINGS[Action.MOVE_UP]:
            if not self.choices.move_up():
                self.focus_next("up")
            else:
                self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.MOVE_DOWN]:
            if not self.choices.move_down():
                self.focus_next("down")
            else:
                self.emit(self.dirty)
            return
        if event.key == KEYBINDINGS[Action.ACTIVATE]:
            self._activate_choice(self.choices.items[self.choices.cursor])
            return

    def _on_key_edit_instructions(self, event: events.Key) -> None:
        if event.key == KEYBINDINGS[Action.MOVE_UP] and self.edit_instructions.cursor == 0:
            self.focus_next("up")
            return
        if event.key == KEYBINDINGS[Action.DISCARD_EDIT_INSTRUCTIONS]:
            # Two escapes within _DOUBLE_ESC_WINDOW seconds discard the buffer;
            # a single escape is a no-op (so accidental presses don't drop work).
            now = time.monotonic()
            if now - self._last_escape_at < _DOUBLE_ESC_WINDOW:
                self.edit_instructions.discard()
                self.emit(self.dirty)
                self._last_escape_at = 0.0
            else:
                self._last_escape_at = now
            return
        # ... text editing keys append to edit_instructions.buffer, etc.

    # ----- topic selection -----

    def _request_topic_selection(self, scope: "TopicScope", entry_idx: int | None) -> None:
        request = self.TopicSelectionRequest(scope=scope, entry_idx=entry_idx)
        self.emit(self.topic_selection_requests, request)

    def apply_topic_selection(
        self,
        request: "TopicSelectionRequest",
        result: tuple[int, str] | None,
    ) -> None:
        """Called by the view after its picker resolves. ``result`` is
        ``(topic_id, topic_name)`` or ``None`` if the user cancelled."""
        if result is None:
            return
        topic_id, topic_name = result
        if request.scope is self.TopicScope.ENTRY and request.entry_idx is not None:
            self._data.set_topic_for_entry(request.entry_idx, topic_id, topic_name)
        elif request.scope is self.TopicScope.ALL:
            self._data.set_topic_for_all(topic_id, topic_name)
        self.emit(self.dirty)

    # ----- choice list orchestration -----

    def _compute_choices(self) -> list[str]:
        """The set of choices visible to the user, derived from the rest of
        the VM state. Called on construction and whenever something that
        affects the list changes (currently: edit-instructions visibility)."""
        if self.edit_instructions.visible:
            # Edit-instructions panel is up: the only meaningful actions left
            # are dismiss/reset/cancel. Approve and request_edits don't apply
            # while the user is composing instructions.
            return ["dismiss_edits", "reset_all", "cancel"]
        return ["accept", "request_edits", "reset_all", "cancel"]

    def _sync_choices(self) -> None:
        """Push the freshly-computed choice list into ``self.choices`` and let
        it re-clamp its cursor against the new length."""
        self.choices.set_items(self._compute_choices())

    # ----- choice/global actions -----

    def _toggle_edit_instructions(self) -> None:
        if self.edit_instructions.visible:
            self.edit_instructions.hide()
            self._sync_choices()
            if self._focused is self.Region.EDIT_INSTRUCTIONS:
                self._focus_region(self.Region.CHOICES_LIST, "up")
        else:
            self.edit_instructions.show()
            self._sync_choices()
            self._focus_region(self.Region.EDIT_INSTRUCTIONS, "down")
        self.emit(self.dirty)

    def _accept_all(self) -> None:
        if self._data.stale_topic_entry_indices():
            # Topic snapshot is out of date relative to one or more entries.
            # Don't commit; let the view refresh and retry. Same gating will
            # apply to the future "submit edit instructions" path.
            self.emit(self.completion_blocked)
            return
        # emit an event / call a callback / set a result flag
        self.emit(self.dirty)

    async def refresh_topics(self) -> None:
        """Re-fetch the topic map from the database and push it into the data
        model. Called by the view on ``DatabaseCommitted`` events affecting
        topics, after deserialization, and in response to ``completion_blocked``
        (to recover from a transiently-stale snapshot before a final retry).

        No-ops when there's no session factory (tests / sample paths)."""
        if self._session_factory is None:
            return
        from rhizome.db import Topic  # local import: avoid pulling DB at module import
        async with self._session_factory() as session:
            result = await session.execute(select(Topic.id, Topic.name))
            topic_map = {tid: name for tid, name in result.all()}
        self._data.replace_topic_map(topic_map)
        self.emit(self.dirty)

    def _cancel(self) -> None:
        self.emit(self.dirty)

    def _activate_choice(self, choice: str) -> None:
        if choice == "accept":
            self._accept_all()
        elif choice == "request_edits":
            self._toggle_edit_instructions()
        elif choice == "reset_all":
            self._data.reset_all()
            self.emit(self.dirty)
        elif choice == "dismiss_edits":
            self.edit_instructions.hide()
            self._sync_choices()
            self.emit(self.dirty)
        elif choice == "cancel":
            self._cancel()
