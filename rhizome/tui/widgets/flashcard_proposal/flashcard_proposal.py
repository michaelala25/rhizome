"""``FlashcardProposal`` — view for ``FlashcardProposalModel``.

Layout (top → bottom):
    shared-topic-setter
    flashcard-list  │  flashcard-details      (horizontal split)
    revision-instructions                      (mounted only in REQUESTING_REVISION)
    menu

Buffers for the per-flashcard question/answer/testing-notes live inside ``FlashcardDetails`` (a
``ContentEditor``); the buffer for the revision feedback lives inside ``RevisionInstructions`` (a
``TextArea``). On confirm, each surface posts a message that lands here and forwards a finalised
value to the VM.

The linked-entry-ids panel inside ``FlashcardDetails`` is a non-editable ``TextAreaParams`` area
— the parent reseeds its text on every cursor move via ``set_text`` for display only; the user
cannot type into it and the focus graph skips over it.

Cursor moves on the flashcard list re-seed the ``FlashcardDetails`` buffers — any in-flight
question/answer/notes edits are silently discarded, matching the commit-proposal convention.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Static, TextArea

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalModel
from rhizome.db import Topic
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.collapse_button import CollapseButton
from rhizome.tui.widgets.shared.content_editor import ContentEditor, TextAreaParams
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.widgets.shared.list_menu import ListMenu, MenuItem
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase

from .flashcard_list import FlashcardList
from .messages import SetTopicRequested


# ========================================================================================================================
# Sub-widgets
# ========================================================================================================================


class SharedTopicSetter(ListMenu):

    SetTopicForAll = MenuItem("Set topic for all", Keybind.ProposalSetTopicAll)
    ITEMS = [SetTopicForAll]
    WRAP = False

    class Sentinel(Enum):
        mixed         = "(mixed)"
        no_flashcards = "(no flashcards)"
        none          = "(none)"

    shared_topic: reactive[Topic | Sentinel] = reactive(Sentinel.none)
    """The shared topic displayed in the hint - either a Topic directly or a Sentinel supplied by the view."""

    def watch_shared_topic(self) -> None:
        self._refresh()

    @property
    def hint(self) -> Text | None:
        if isinstance(self.shared_topic, self.Sentinel):
            return Text(f"Current Topic: {self.shared_topic.value}", style="dim")
        return Text(f"Current Topic: {self.shared_topic.name}", style="dim")

    @on(ListMenu.Selected)
    def _on_selected(self, event: ListMenu.Selected):
        event.stop()  # So we don't bubble to the FlashcardProposal
        # Only one item to select
        self.post_message(SetTopicRequested("all"))


class FlashcardDetails(ContentEditor):
    """Question + Answer + Testing-Notes editable areas, plus a read-only Linked Entries area, with
    the shared Accept/Discard menu from ``ContentEditor``. The Linked Entries area is declared with
    ``editable=False`` so it sits in the layout for display only — out of the alt+arrow nav graph,
    out of dirty detection."""

    class Areas:
        Question      = "Question"
        Answer        = "Answer"
        TestingNotes  = "Testing Notes"
        LinkedEntries = "Linked Knowledge Entries"

    AREAS = {
        Areas.Question:      TextAreaParams(kwargs={"id": "fp-flashcard-details-question"}),
        Areas.Answer:        TextAreaParams(kwargs={"id": "fp-flashcard-details-answer"}),
        Areas.TestingNotes:  TextAreaParams(kwargs={"id": "fp-flashcard-details-testing-notes"}),
        Areas.LinkedEntries: TextAreaParams(
            editable=False,
            kwargs={"id": "fp-flashcard-details-linked-entries"},
        ),
    }


class RevisionInstructions(TextArea):
    """Free-text feedback area shown only in ``REQUESTING_REVISION``. Owns its buffer view-side
    until the parent reads it on submit."""

    can_focus = True

    DEFAULT_CSS = """
    RevisionInstructions {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    RevisionInstructions:focus {
        border: solid $accent;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(show_line_numbers=False, soft_wrap=True, **kwargs)
        self.border_title = "Revision feedback"

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "cursor_up":
            return self.cursor_location[0] > 0
        if action == "cursor_down":
            return self.cursor_location[0] < self.document.line_count - 1
        return True


class FlashcardProposalMenu(ListMenu):
    """Vertical action menu. Item list switches on state — ``set_state`` from the parent."""

    Accept          = MenuItem("Accept",           Keybind.ProposalAcceptAll,              "approve, applying any user edits")
    RequestRevision = MenuItem("Request Revision", Keybind.ProposalToggleEditInstructions, "open the revision feedback area")
    SubmitRevision  = MenuItem("Submit Revision",  Keybind.ProposalSubmitRevision,         "send the feedback to the agent")
    CancelRevision  = MenuItem("Cancel Revision",  Keybind.ProposalToggleEditInstructions, "close the feedback area, return to review")
    Reset           = MenuItem("Reset",            Keybind.ProposalReset,                  "reset all user edits")
    Cancel          = MenuItem("Cancel",           Keybind.ProposalCancel,                 "reject the proposal entirely")

    ORIENTATION = "vertical"

    DEFAULT_CSS = """
    FlashcardProposalMenu {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
    }
    FlashcardProposalMenu:focus {
        border-top: solid $accent;
    }
    """

    WRAP = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state: FlashcardProposalModel.State = FlashcardProposalModel.State.REVIEWING

    def set_state(self, state: FlashcardProposalModel.State) -> None:
        if self._state == state:
            return
        self._state = state
        self.cursor = 0
        self._refresh()

    @property
    def items(self) -> list[MenuItem]:
        if self._state == FlashcardProposalModel.State.REVIEWING:
            return [self.Accept, self.RequestRevision, self.Reset, self.Cancel]
        if self._state == FlashcardProposalModel.State.REQUESTING_REVISION:
            return [self.SubmitRevision, self.CancelRevision, self.Reset, self.Cancel]
        return []


# ========================================================================================================================
# Root widget
# ========================================================================================================================

_TITLE_RED      = "rgb(255,80,80)"
_APPROVED_GREEN = "rgb(120,210,110)"
_REVISED_YELLOW = "rgb(235,180,90)"
_CANCEL_RED     = "rgb(235,100,100)"

_DESCRIPTION = (
    "The agent has drafted these flashcards to commit — edit them inline, exclude any you don't "
    "want, or request revisions before approving."
)

_OUTCOME_LABEL = {
    FlashcardProposalModel.Outcome.ACCEPTED:  f"[bold {_APPROVED_GREEN}]approved[/]",
    FlashcardProposalModel.Outcome.REVISED:   f"[bold {_REVISED_YELLOW}]revised[/]",
    FlashcardProposalModel.Outcome.CANCELLED: f"[bold {_CANCEL_RED}]cancelled[/]",
}


class FlashcardProposal(NavigableFeedItemViewBase[FlashcardProposalModel], FocusOrchestrationMixin):
    """Parent view for the flashcard-proposal interrupt."""

    can_focus = True

    DEFAULT_CSS = """
    FlashcardProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        padding: 0 1;
    }
    FlashcardProposal #fp-title-row {
        height: 1;
        background: transparent;
    }
    FlashcardProposal #fp-title {
        width: 1fr;
        height: 1;
        background: transparent;
    }
    FlashcardProposal #fp-collapse {
        dock: right;
        display: none;
    }
    FlashcardProposal.-done #fp-collapse {
        display: block;
    }

    /* DONE visibility — menu and revision-instructions are gone for good once we're in DONE. */
    FlashcardProposal.-done #fp-menu,
    FlashcardProposal.-done #fp-revision-instructions {
        display: none;
    }

    /* Collapsed-DONE strips everything but the title-row + summary line. */
    FlashcardProposal.-collapsed #fp-title,
    FlashcardProposal.-collapsed #fp-description,
    FlashcardProposal.-collapsed #fp-shared-topic-setter,
    FlashcardProposal.-collapsed #fp-middle {
        display: none;
    }

    /* Centered outcome label — replaces the menu in expanded-DONE. */
    FlashcardProposal #fp-done-status {
        height: 3;
        content-align: center middle;
        background: transparent;
        display: none;
    }
    FlashcardProposal.-done #fp-done-status {
        display: block;
    }
    FlashcardProposal.-done.-collapsed #fp-done-status {
        display: none;
    }

    /* Centered "Flashcard Proposal - (N flashcards) - <outcome>" line, collapsed-DONE only. */
    FlashcardProposal #fp-done-summary-line {
        height: 1;
        content-align: center middle;
        background: transparent;
        display: none;
    }
    FlashcardProposal.-done.-collapsed #fp-done-summary-line {
        display: block;
    }

    /* Dim-grey readout of the revision feedback — shown in DONE (both expanded and collapsed)
       iff the outcome is REVISED. Visibility is flipped from ``_on_done``. */
    FlashcardProposal #fp-done-instructions {
        margin: 1 4 0 4;
        padding: 1 2;
        height: auto;
        background: rgb(40,40,40);
        color: rgb(180,180,180);
        display: none;
    }
    FlashcardProposal #fp-description {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: #707070;
    }
    FlashcardProposal #fp-shared-topic-setter {
        margin-top: 1;
    }
    FlashcardProposal #fp-middle {
        height: auto;
        background: transparent;
    }
    FlashcardProposal #fp-flashcard-list-area {
        width: 3fr;
        height: auto;
        max-height: 25;
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
    }
    FlashcardProposal #fp-flashcard-list-hints {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    FlashcardProposal #fp-flashcard-details {
        width: 2fr;
        height: auto;
        padding: 0 1;
    }
    /* Read-only Linked-Knowledge-Entries pane: dimmer border + text, distinguishing it from the
       editable areas above. */
    FlashcardProposal #fp-flashcard-details-linked-entries {
        border: solid #2a2a2a;
        border-title-color: rgb(100,100,100);
        color: rgb(140,140,140);
        min-height: 1;
        max-height: 4;
    }
    """

    BINDINGS = [
        Keybind.InnerFocusUp.   as_binding("focus_neighbour('up')",    show=False),
        Keybind.InnerFocusDown. as_binding("focus_neighbour('down')",  show=False),
        Keybind.InnerFocusLeft. as_binding("focus_neighbour('left')",  show=False),
        Keybind.InnerFocusRight.as_binding("focus_neighbour('right')", show=False),
        Keybind.CursorUp.  as_binding("navigate_cursor('up')",    show=False),
        Keybind.CursorDown.as_binding("navigate_cursor('down')",  show=False),

        Keybind.ProposalAcceptAll.             as_binding("accept",              show=False),
        # Remark: these take priority because their default keys conflict with TextArea builtins that we want
        # to override. Accept's default key is "ctrl+a" which we want to leave as "select all" in text areas.
        Keybind.ProposalSubmitRevision.        as_binding("submit_revision",     show=False, priority=True),
        Keybind.ProposalToggleEditInstructions.as_binding("toggle_revision",     show=False, priority=True),
        Keybind.ProposalReset.                 as_binding("reset",               show=False, priority=True),
        Keybind.ProposalCancel.                as_binding("cancel",              show=False, priority=True),
        # Default key is "shift+t" - don't want to fire this while focused in a TextArea, so no priority here.
        Keybind.ProposalSetTopicAll.           as_binding("set_topic_all",       show=False),
        Keybind.ProposalToggleCollapsed.       as_binding("toggle_collapsed",    show=False),

        # ``priority`` so escape beats the menu's own CloseMenu binding (which would otherwise
        # consume it and just post Dismiss). ``check_action`` gates this off in non-revising
        # states so the menu's escape behaves normally elsewhere.
        Keybind.CloseMenu.as_binding("cancel_revision", show=False, priority=True),
    ]

    FOCUS_GRAPH = FocusGraph(
        source="fp-flashcard-list",
        edges={
            "fp-shared-topic-setter": {
                "down": "fp-flashcard-list",
            },
            "fp-flashcard-list": {
                "up":    "fp-shared-topic-setter",
                "down":  ["fp-revision-instructions", "fp-menu"],
                "right": "fp-flashcard-details",
            },
            "fp-flashcard-details": {
                "up":    "fp-flashcard-list",
                "left":  "fp-flashcard-list",
                "down":  ["fp-revision-instructions", "fp-menu"],
            },
            "fp-revision-instructions": {
                "up":   "fp-flashcard-list",
                "down": "fp-menu",
            },
            "fp-menu": {
                "up": ["fp-revision-instructions", "fp-flashcard-list"],
            },
        },
    )

    collapsed: reactive[bool] = reactive(False)
    """DONE-only view-side flag. Drives the ``-collapsed`` CSS class which hides everything but
    the title row + collapse button. Initialised ``False``; auto-flipped to ``True`` on the first
    ``OnDone`` emit so a freshly-resolved proposal lands collapsed."""

    def __init__(self, vm: FlashcardProposalModel, **kwargs: Any) -> None:
        super().__init__(vm, **kwargs)
        self._session_factory = vm.session_factory

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        with Horizontal(id="fp-title-row"):
            yield Static(self._title_text(), id="fp-title")
            yield CollapseButton("▼", id="fp-collapse")

        yield Static(_DESCRIPTION, id="fp-description")

        yield SharedTopicSetter(id="fp-shared-topic-setter")

        with Horizontal(id="fp-middle"):
            list_area = Vertical(id="fp-flashcard-list-area")
            list_area.border_title = "Flashcards"
            with list_area:
                yield FlashcardList(self.model, id="fp-flashcard-list")
                yield Static(self._flashcard_list_hints_text(), id="fp-flashcard-list-hints")

            yield FlashcardDetails(id="fp-flashcard-details")

        yield FlashcardProposalMenu(id="fp-menu")

        yield Static("", id="fp-done-status")
        yield Static("", id="fp-done-summary-line")
        yield Static("", id="fp-done-instructions")

    def watch_collapsed(self, collapsed: bool) -> None:
        self.set_class(collapsed, "-collapsed")
        try:
            self.query_one("#fp-collapse", CollapseButton).update("▶" if collapsed else "▼")
        except Exception:
            pass

        # Land focus on the right node for the new mode. On expand we go straight to focus_first
        # — self is already focused (we landed there when we collapsed), so self.focus() would be
        # a no-op and on_focus wouldn't re-fire to delegate inward.
        if collapsed:
            self.focus()
        else:
            self.focus_first()

    @on(CollapseButton.Pressed)
    def _on_collapse_pressed(self, event: CollapseButton.Pressed) -> None:
        if self.model.is_done:
            self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.model.subscribe(self.model.Callbacks.OnFlashcardsChanged, self._on_flashcards_changed)
        self.model.subscribe(self.model.Callbacks.OnRevisingChanged,   self._on_revising_changed)
        self.model.subscribe(self.model.Callbacks.OnDone,              self._on_done)

        self._sync()
        self._sync_list_area_height()

        if self.model.state == FlashcardProposalModel.State.REVIEWING:
            self.focus_first()

    def on_unmount(self) -> None:
        self.model.unsubscribe(self.model.Callbacks.OnFlashcardsChanged, self._on_flashcards_changed)
        self.model.unsubscribe(self.model.Callbacks.OnRevisingChanged,   self._on_revising_changed)
        self.model.unsubscribe(self.model.Callbacks.OnDone,              self._on_done)

    def on_resize(self) -> None:
        self._sync_list_area_height()

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_list_area_height()

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def focus_first(self) -> str | None:
        if self.model.is_done and self.collapsed:
            # Self already focused, nothing to do - calling self.focus() here would cause an infinite loop
            return None

        widget = self._resolve_node("fp-flashcard-list")
        if widget is None:
            return None
        widget.focus()
        return "fp-flashcard-list"

    def action_focus_neighbour(self, direction: str) -> None:
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    def _is_node_available(self, node_id: str) -> bool:
        # DONE clamps the focus graph to the flashcard list — the user browses but can't edit.
        if self.model.is_done:
            return node_id == "fp-flashcard-list"
        if node_id == "fp-revision-instructions":
            return self.model.is_revising
        return True

    # Cursor navigation walks a subset of the focus graph — flashcard-details is excluded so plain
    # up/down inside a TextArea still moves the caret instead of leaving the widget.
    _CURSOR_NAV_NODES = frozenset({
        "fp-shared-topic-setter",
        "fp-flashcard-list",
        "fp-menu",
        "fp-revision-instructions",
    })

    def action_navigate_cursor(self, direction: str) -> None:
        if self._current_focus_node() not in self._CURSOR_NAV_NODES:
            raise SkipAction()

        if (target_id := self.focus_neighbour(direction)) is None:
            raise SkipAction()

        flashcard_list = self._flashcard_list()
        menu = self._menu()

        if direction == "up":
            if target_id == flashcard_list.id:
                flashcard_list.cursor_coordinate = Coordinate(len(self.model.flashcards) - 1, 0)
            elif target_id == menu.id:
                menu.cursor = len(menu.items) - 1
        elif direction == "down":
            if target_id == flashcard_list.id:
                flashcard_list.cursor_coordinate = Coordinate(0, 0)
            elif target_id == menu.id:
                menu.cursor = 0

    # ------------------------------------------------------------------
    # Keystroke actions (also check_action-gated by state)
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        state = self.model.state

        if action == "accept":
            return state == FlashcardProposalModel.State.REVIEWING
        if action == "submit_revision":
            return state == FlashcardProposalModel.State.REQUESTING_REVISION
        if action == "cancel_revision":
            return state == FlashcardProposalModel.State.REQUESTING_REVISION
        if action == "toggle_collapsed":
            return state == FlashcardProposalModel.State.DONE
        if action in ("toggle_revision", "reset", "cancel", "set_topic_all"):
            return state != FlashcardProposalModel.State.DONE
        return True

    def action_accept(self) -> None:
        self.model.accept()

    def action_submit_revision(self) -> None:
        assert (revision_area := self._revision_text_area()) is not None
        self.model.submit_revision(revision_area.text)

    def action_toggle_revision(self) -> None:
        if self.model.state == FlashcardProposalModel.State.REVIEWING:
            self.model.request_revision()
        elif self.model.state == FlashcardProposalModel.State.REQUESTING_REVISION:
            self.model.cancel_revision()

    def action_reset(self) -> None:
        self.model.reset()

    def action_cancel(self) -> None:
        self.model.cancel()

    def action_set_topic_all(self) -> None:
        self._open_topic_picker(scope="all")

    def action_cancel_revision(self) -> None:
        self.model.cancel_revision()

    def action_toggle_collapsed(self) -> None:
        self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Menu → action dispatch
    # ------------------------------------------------------------------

    @on(FlashcardProposalMenu.Selected)
    def _on_menu_selected(self, event: FlashcardProposalMenu.Selected) -> None:
        item = event.item
        if item is FlashcardProposalMenu.Accept:
            self.action_accept()
        elif item is FlashcardProposalMenu.RequestRevision:
            self.action_toggle_revision()
        elif item is FlashcardProposalMenu.SubmitRevision:
            self.action_submit_revision()
        elif item is FlashcardProposalMenu.CancelRevision:
            self.action_toggle_revision()
        elif item is FlashcardProposalMenu.Reset:
            self.action_reset()
        elif item is FlashcardProposalMenu.Cancel:
            self.action_cancel()

    # ------------------------------------------------------------------
    # Flashcard list → details panel
    # ------------------------------------------------------------------

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Remark: row highlighting doesn't change the shared topic, don't need to update it here.
        self._sync_flashcard_details()

    def _on_flashcards_changed(self, indices: list[int]) -> None:
        self._sync()

    def _sync(self):
        self._sync_flashcard_details()
        self._sync_shared_topic_setter()

    def _sync_flashcard_details(self) -> None:
        flashcard_list = self._flashcard_list()
        idx = flashcard_list.cursor_row

        flashcard = self.model.flashcards[idx]
        details = self._flashcard_details()
        details.set_text(FlashcardDetails.Areas.Question,     flashcard.question)
        details.set_text(FlashcardDetails.Areas.Answer,       flashcard.answer)
        details.set_text(FlashcardDetails.Areas.TestingNotes, flashcard.testing_notes)
        details.set_text(
            FlashcardDetails.Areas.LinkedEntries,
            self._format_linked_entries(flashcard.entry_ids),
        )

    def _format_linked_entries(self, ids: list[int]) -> str:
        if not ids:
            return "(none)"
        return ", ".join(f"#{i}" for i in ids)

    def _sync_shared_topic_setter(self) -> None:
        shared_topic_setter = self._shared_topic_setter()

        flashcards = self.model.flashcards

        if not flashcards:
            shared_topic_setter.shared_topic = SharedTopicSetter.Sentinel.no_flashcards
            return

        shared_topic: Topic | None = None
        shared: bool = True
        for flashcard in flashcards:
            if shared_topic is None:
                shared_topic = flashcard.topic

            # Note: a little scared of direct comparison between Topic objects, compare by
            # None-ness/id instead.
            if (
                (flashcard.topic is None and shared_topic is not None) or
                (flashcard.topic is not None and shared_topic is None) or
                (flashcard.topic.id != shared_topic.id)
            ):
                shared = False
                break

        if shared:
            if shared_topic is None:
                shared_topic_setter.shared_topic = SharedTopicSetter.Sentinel.none
            else:
                shared_topic_setter.shared_topic = shared_topic
        else:
            shared_topic_setter.shared_topic = SharedTopicSetter.Sentinel.mixed

    # ------------------------------------------------------------------
    # Flashcard details → VM
    # ------------------------------------------------------------------

    @on(ContentEditor.ChangesAccepted)
    def _on_changes_accepted(self, event: ContentEditor.ChangesAccepted) -> None:

        flashcard_list = self._flashcard_list()
        idx = flashcard_list.cursor_row

        areas = event.text_areas
        if FlashcardDetails.Areas.Question in areas:
            self.model.set_flashcard_question(idx, areas[FlashcardDetails.Areas.Question].text)
        if FlashcardDetails.Areas.Answer in areas:
            self.model.set_flashcard_answer(idx, areas[FlashcardDetails.Areas.Answer].text)
        if FlashcardDetails.Areas.TestingNotes in areas:
            self.model.set_flashcard_testing_notes(idx, areas[FlashcardDetails.Areas.TestingNotes].text)

        details = self._flashcard_details()
        flashcard = self.model.flashcards[idx]
        details.set_text(FlashcardDetails.Areas.Question,     flashcard.question)
        details.set_text(FlashcardDetails.Areas.Answer,       flashcard.answer)
        details.set_text(FlashcardDetails.Areas.TestingNotes, flashcard.testing_notes)

    # ------------------------------------------------------------------
    # Revision state mount / unmount
    # ------------------------------------------------------------------

    def _on_revising_changed(self, revising: bool) -> None:
        menu = self._menu()
        menu.set_state(self.model.state)

        if revising:
            self._mount_revision_instructions()
        else:
            self._unmount_revision_instructions()

    def _mount_revision_instructions(self) -> None:
        try:
            self.query_one("#fp-revision-instructions", RevisionInstructions)
            return
        except Exception:
            pass

        widget = RevisionInstructions(id="fp-revision-instructions")
        self.mount(widget, before=self._menu())
        widget.focus()

    def _unmount_revision_instructions(self) -> None:
        try:
            widget = self.query_one("#fp-revision-instructions", RevisionInstructions)
        except Exception:
            return
        widget.remove()

    def _on_done(self, outcome: FlashcardProposalModel.Outcome) -> None:
        self._menu().set_state(self.model.state)
        self.set_class(True, "-done")
        self.collapsed = True

        # Populate the DONE-only status surfaces. Both stay in the DOM (CSS gates visibility); the
        # outcome doesn't change after this so we set it once here.
        label = _OUTCOME_LABEL[outcome]
        self.query_one("#fp-done-status",       Static).update(label)
        self.query_one("#fp-done-summary-line", Static).update(self._summary_text(label))

        feedback = (self.model.revision_feedback or "").strip()
        instructions_widget = self.query_one("#fp-done-instructions", Static)
        if feedback:
            instructions_widget.update(feedback)
            instructions_widget.display = True

        # Drop every editable descendant out of the screen-level focus chain. ``_is_node_available``
        # already handles our own alt+arrow graph; this keeps a stale TextArea from soaking up
        # programmatic focus from the chat pane's post-resolve refocus.
        for widget in self.query(Widget):
            if widget.id == "fp-flashcard-list":
                continue
            widget.can_focus = False

        # Blur any currently-focused descendant — the chat pane is about to refocus its input.
        if self.screen is not None and self.screen.focused is not None:
            focused = self.screen.focused
            node = focused
            while node is not None and node is not self:
                node = node.parent
            if node is self:
                focused.blur()

    # ------------------------------------------------------------------
    # Topic picker
    # ------------------------------------------------------------------

    @on(SetTopicRequested)
    def _on_set_topic_requested(self, event: SetTopicRequested) -> None:
        if self.model.is_done:
            return
        self._open_topic_picker(scope=event.scope)
        event.stop()

    def _open_topic_picker(self, *, scope: str) -> None:
        if self._session_factory is None:
            return

        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        def _on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                return
            topic_id, topic_name = result
            topic = Topic(id=topic_id, name=topic_name)

            flashcard_list = self._flashcard_list()
            idx = flashcard_list.cursor_row
            if scope == "current":
                self.model.set_flashcard_topic(idx, topic)
            elif scope == "all":
                self.model.set_topic_all(topic)

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._session_factory),
            _on_dismiss,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _shared_topic_setter(self) -> SharedTopicSetter:
        return self.query_one("#fp-shared-topic-setter", SharedTopicSetter)

    def _flashcard_list(self) -> FlashcardList:
        return self.query_one("#fp-flashcard-list", FlashcardList)

    def _flashcard_details(self) -> FlashcardDetails:
        return self.query_one("#fp-flashcard-details", FlashcardDetails)

    # ContentEditorMenu height + its margin-top — reserved in the area's height even when the
    # menu is hidden, so the layout doesn't jump when the buffer becomes dirty.
    _DETAILS_MENU_RESERVED = 4

    # Matches the ``max-height`` on ``#fp-flashcard-list-area`` — keeps the border + hint from
    # marching off-screen when the flashcard count goes wild (e.g. ``--big``).
    _AREA_MAX_HEIGHT = 25

    def _sync_list_area_height(self) -> None:
        def _apply() -> None:
            if self.model.is_done:
                return
            try:
                area    = self.query_one("#fp-flashcard-list-area", Vertical)
                lst     = self._flashcard_list()
                details = self._flashcard_details()
            except Exception:
                return

            natural   = lst.virtual_size.height + 1 + 2
            details_h = details.virtual_size.height + (0 if details.dirty else self._DETAILS_MENU_RESERVED)
            area.styles.height = min(max(natural, details_h), self._AREA_MAX_HEIGHT)

        self.call_after_refresh(_apply)

    def _menu(self) -> FlashcardProposalMenu:
        return self.query_one("#fp-menu", FlashcardProposalMenu)

    def _revision_text_area(self) -> RevisionInstructions | None:
        try:
            return self.query_one("#fp-revision-instructions", RevisionInstructions)
        except Exception:
            return None

    def _title_text(self) -> Text:
        n = len(self.model.flashcards)
        noun = "flashcard" if n == 1 else "flashcards"
        text = Text()
        text.append("Flashcard Proposal", style=f"bold {_TITLE_RED}")
        text.append(f" - ({n} {noun})", style="dim")
        return text

    def _summary_text(self, outcome_label: str) -> str:
        n = len(self.model.flashcards)
        noun = "flashcard" if n == 1 else "flashcards"
        return (
            f"[bold {_TITLE_RED}]Flashcard Proposal[/]  "
            f"[dim]- ({n} {noun}) -[/]  {outcome_label}"
        )

    def _flashcard_list_hints_text(self) -> str:
        rows = [
            (Keybind.ProposalSetTopic.default_key,      "set topic"),
            (Keybind.ProposalSetTopicAll.default_key,   "set topic for all"),
            (Keybind.ProposalToggleExclude.default_key, "exclude"),
            ("alt+←↑→↓",                                "navigate"),
        ]
        return "   ".join(f"[#a0a0a0]{k}[/] [#707070]{label}[/]" for k, label in rows)
