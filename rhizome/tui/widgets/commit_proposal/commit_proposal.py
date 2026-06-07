"""``CommitProposal`` — view for ``CommitProposalModel``.

Layout (top → bottom):
    shared-topic-setter
    entry-list  │  entry-details          (horizontal split)
    revision-instructions                  (mounted only in REQUESTING_REVISION)
    menu

Buffers for the per-entry title/content live inside ``EntryDetails`` (a ``ContentEditor``); the
buffer for the revision feedback lives inside ``RevisionInstructions`` (a ``TextArea``). On
confirm, each surface posts a message that lands here and forwards a finalised value to the VM.

Cursor moves on the entry list re-seed the ``EntryDetails`` buffers — any in-flight title/content
edits are silently discarded, matching the existing widget's convention.
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

from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel
from rhizome.db import Topic
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase

from rhizome.tui.widgets.shared.collapse_button import CollapseButton
from rhizome.tui.widgets.shared.content_editor import ContentEditor, TextAreaParams
from rhizome.tui.widgets.shared.list_menu import ListMenu, MenuItem

from .entry_list import EntryList
from .messages import SetTopicRequested


# ========================================================================================================================
# Sub-widgets
# ========================================================================================================================


class SharedTopicSetter(ListMenu):

    SetTopicForAll = MenuItem("Set topic for all", Keybind.ProposalSetTopicAll)
    ITEMS = [SetTopicForAll]
    WRAP = False

    class Sentinel(Enum):
        mixed = "(mixed)"
        no_entries = "(no entries)"
        none = "(none)"

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
        event.stop() # So we don't bubble to the CommitProposal
        # Only one item to select
        self.post_message(SetTopicRequested("all"))
    


class EntryDetails(ContentEditor):
    """Title + Content TextAreas with the shared Accept/Discard menu from ``ContentEditor``."""

    class Areas:
        Title   = "Title"
        Content = "Content"

    AREAS = {
        Areas.Title:   TextAreaParams(kwargs={"id": "cp-entry-details-title"}),
        Areas.Content: TextAreaParams(kwargs={"id": "cp-entry-details-content"}),
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


class CommitProposalMenu(ListMenu):
    """Vertical action menu. Item list switches on state — ``set_state`` from the parent."""

    Accept          = MenuItem("Accept",           Keybind.ProposalAcceptAll,              "approve, applying any user edits")
    RequestRevision = MenuItem("Request Revision", Keybind.ProposalToggleEditInstructions, "open the revision feedback area")
    SubmitRevision  = MenuItem("Submit Revision",  Keybind.ProposalSubmitRevision,         "send the feedback to the agent")
    CancelRevision  = MenuItem("Cancel Revision",  Keybind.ProposalToggleEditInstructions, "close the feedback area, return to review")
    Reset           = MenuItem("Reset",            Keybind.ProposalReset,                  "reset all user edits")
    Cancel          = MenuItem("Cancel",           Keybind.ProposalCancel,                 "reject the proposal entirely")

    ORIENTATION = "vertical"

    DEFAULT_CSS = """
    CommitProposalMenu {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
    }
    CommitProposalMenu:focus {
        border-top: solid $accent;
    }
    """

    WRAP = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state: CommitProposalModel.State = CommitProposalModel.State.REVIEWING

    def set_state(self, state: CommitProposalModel.State) -> None:
        if self._state == state:
            return
        self._state = state
        self.cursor = 0
        self._refresh()

    @property
    def items(self) -> list[MenuItem]:
        if self._state == CommitProposalModel.State.REVIEWING:
            return [self.Accept, self.RequestRevision, self.Reset, self.Cancel]
        if self._state == CommitProposalModel.State.REQUESTING_REVISION:
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
    "The agent has drafted these knowledge entries to commit — edit them inline, exclude any you "
    "don't want, or request revisions before approving."
)

_OUTCOME_LABEL = {
    CommitProposalModel.Outcome.ACCEPTED:  f"[bold {_APPROVED_GREEN}]approved[/]",
    CommitProposalModel.Outcome.REVISED:   f"[bold {_REVISED_YELLOW}]revised[/]",
    CommitProposalModel.Outcome.CANCELLED: f"[bold {_CANCEL_RED}]cancelled[/]",
}


class CommitProposal(NavigableFeedItemViewBase[CommitProposalModel], FocusOrchestrationMixin):
    """Parent view for the commit-proposal interrupt."""

    can_focus = True

    DEFAULT_CSS = """
    CommitProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        padding: 0 1;
    }
    CommitProposal #cp-title-row {
        height: 1;
        background: transparent;
    }
    CommitProposal #cp-title {
        width: 1fr;
        height: 1;
        background: transparent;
    }
    CommitProposal #cp-collapse {
        dock: right;
        display: none;
    }
    CommitProposal.-done #cp-collapse {
        display: block;
    }

    /* DONE visibility — menu and revision-instructions are gone for good once we're in DONE. */
    CommitProposal.-done #cp-menu,
    CommitProposal.-done #cp-revision-instructions {
        display: none;
    }

    /* Collapsed-DONE strips everything but the title-row + summary line. */
    CommitProposal.-collapsed #cp-title,
    CommitProposal.-collapsed #cp-description,
    CommitProposal.-collapsed #cp-shared-topic-setter,
    CommitProposal.-collapsed #cp-middle {
        display: none;
    }

    /* Centered outcome label — replaces the menu in expanded-DONE. */
    CommitProposal #cp-done-status {
        height: 3;
        content-align: center middle;
        background: transparent;
        display: none;
    }
    CommitProposal.-done #cp-done-status {
        display: block;
    }
    CommitProposal.-done.-collapsed #cp-done-status {
        display: none;
    }

    /* Centered "Commit Proposal - (N entries) - <outcome>" line, collapsed-DONE only. */
    CommitProposal #cp-done-summary-line {
        height: 1;
        content-align: center middle;
        background: transparent;
        display: none;
    }
    CommitProposal.-done.-collapsed #cp-done-summary-line {
        display: block;
    }

    /* Dim-grey readout of the revision feedback — shown in DONE (both expanded and collapsed)
       iff the outcome is REVISED. Visibility is flipped from ``_on_done``. */
    CommitProposal #cp-done-instructions {
        margin: 1 4 0 4;
        padding: 1 2;
        height: auto;
        background: rgb(40,40,40);
        color: rgb(180,180,180);
        display: none;
    }
    CommitProposal #cp-description {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: #707070;
    }
    CommitProposal #cp-shared-topic-setter {
        margin-top: 1;
    }
    CommitProposal #cp-middle {
        height: auto;
        background: transparent;
    }
    CommitProposal #cp-entry-list-area {
        width: 3fr;
        height: auto;
        max-height: 25;
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
    }
    CommitProposal #cp-entry-list-hints {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    CommitProposal #cp-entry-details {
        width: 2fr;
        height: auto;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Keybind.FocusUp.   as_binding("focus_neighbour('up')",    show=False),
        Keybind.FocusDown. as_binding("focus_neighbour('down')",  show=False),
        Keybind.FocusLeft. as_binding("focus_neighbour('left')",  show=False),
        Keybind.FocusRight.as_binding("focus_neighbour('right')", show=False),
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
        source="cp-entry-list",
        edges={
            "cp-shared-topic-setter": {
                "down": "cp-entry-list",
            },
            "cp-entry-list": {
                "up":    "cp-shared-topic-setter",
                "down":  ["cp-revision-instructions", "cp-menu"],
                "right": "cp-entry-details",
            },
            "cp-entry-details": {
                "up":    "cp-entry-list",
                "left":  "cp-entry-list",
                "down":  ["cp-revision-instructions", "cp-menu"],
            },
            "cp-revision-instructions": {
                "up":   "cp-entry-list",
                "down": "cp-menu",
            },
            "cp-menu": {
                "up": ["cp-revision-instructions", "cp-entry-list"],
            },
        },
    )


    collapsed: reactive[bool] = reactive(False)
    """DONE-only view-side flag. Drives the ``-collapsed`` CSS class which hides everything but
    the title row + collapse button. Initialised ``False``; auto-flipped to ``True`` on the first
    ``OnDone`` emit so a freshly-resolved proposal lands collapsed."""


    def __init__(self, vm: CommitProposalModel, **kwargs: Any) -> None:
        super().__init__(vm, **kwargs)
        self._session_factory = vm.session_factory

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        with Horizontal(id="cp-title-row"):
            yield Static(self._title_text(), id="cp-title")
            yield CollapseButton("▼", id="cp-collapse")

        yield Static(_DESCRIPTION, id="cp-description")

        yield SharedTopicSetter(id="cp-shared-topic-setter")

        with Horizontal(id="cp-middle"):
            entry_area = Vertical(id="cp-entry-list-area")
            entry_area.border_title = "Entries"
            with entry_area:
                yield EntryList(self.model, id="cp-entry-list")
                yield Static(self._entry_list_hints_text(), id="cp-entry-list-hints")

            yield EntryDetails(id="cp-entry-details")

        yield CommitProposalMenu(id="cp-menu")

        yield Static("", id="cp-done-status")
        yield Static("", id="cp-done-summary-line")
        yield Static("", id="cp-done-instructions")

    def watch_collapsed(self, collapsed: bool) -> None:
        self.set_class(collapsed, "-collapsed")
        try:
            self.query_one("#cp-collapse", CollapseButton).update("▶" if collapsed else "▼")
        except Exception:
            pass

        # Re-ping focus_first() to toggle focus between self (outer widget) when collapsed, and
        # entry-list when expanded.
        self.focus()

    @on(CollapseButton.Pressed)
    def _on_collapse_pressed(self, event: CollapseButton.Pressed) -> None:
        if self.model.is_done:
            self.collapsed = not self.collapsed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.model.subscribe(self.model.Callbacks.OnEntriesChanged,  self._on_entries_changed)
        self.model.subscribe(self.model.Callbacks.OnRevisingChanged, self._on_revising_changed)
        self.model.subscribe(self.model.Callbacks.OnDone,            self._on_done)

        self._sync()
        self._sync_entry_area_height()

        if self.model.state == CommitProposalModel.State.REVIEWING:
            self.focus_first()

    def on_unmount(self) -> None:
        self.model.unsubscribe(self.model.Callbacks.OnEntriesChanged,  self._on_entries_changed)
        self.model.unsubscribe(self.model.Callbacks.OnRevisingChanged, self._on_revising_changed)
        self.model.unsubscribe(self.model.Callbacks.OnDone,            self._on_done)

    def on_resize(self) -> None:
        self._sync_entry_area_height()

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_entry_area_height()

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def focus_first(self) -> str | None:
        if self.model.is_done and self.collapsed:
            # Self already focused, nothing to do - calling self.focus() here would cause an infinite loop
            return None
        
        widget = self._resolve_node("cp-entry-list")
        if widget is None:
            return None
        widget.focus()
        return "cp-entry-list"

    def action_focus_neighbour(self, direction: str) -> None:
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    def _is_node_available(self, node_id: str) -> bool:
        # DONE clamps the focus graph to the entry list — the user browses but can't edit.
        if self.model.is_done:
            return node_id == "cp-entry-list"
        if node_id == "cp-revision-instructions":
            return self.model.is_revising
        return True

    # Cursor navigation walks a subset of the focus graph — entry-details is excluded so plain
    # up/down inside a TextArea still moves the caret instead of leaving the widget.
    _CURSOR_NAV_NODES = frozenset({
        "cp-shared-topic-setter",
        "cp-entry-list",
        "cp-menu",
        "cp-revision-instructions",
    })

    def action_navigate_cursor(self, direction: str) -> None:
        if self._current_focus_node() not in self._CURSOR_NAV_NODES:
            raise SkipAction()

        if (target_id := self.focus_neighbour(direction)) is None:
            raise SkipAction()

        entry_list = self._entry_list()
        menu = self._menu()

        # Reset the target widget's cursor to the side it was entered from — bottom row when
        # arriving via "up" (you came from below), top row when arriving via "down".
        if direction == "up":
            if target_id == entry_list.id:
                entry_list.cursor_coordinate = Coordinate(len(self.model.entries) - 1, 0)
            elif target_id == menu.id:
                menu.cursor = len(menu.items) - 1
        elif direction == "down":
            if target_id == entry_list.id:
                entry_list.cursor_coordinate = Coordinate(0, 0)
            elif target_id == menu.id:
                menu.cursor = 0

    # ------------------------------------------------------------------
    # Keystroke actions (also check_action-gated by state)
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        state = self.model.state

        if action == "accept":
            return state == CommitProposalModel.State.REVIEWING
        if action == "submit_revision":
            return state == CommitProposalModel.State.REQUESTING_REVISION
        if action == "cancel_revision":
            return state == CommitProposalModel.State.REQUESTING_REVISION
        if action == "toggle_collapsed":
            return state == CommitProposalModel.State.DONE
        if action in ("toggle_revision", "reset", "cancel", "set_topic_all"):
            return state != CommitProposalModel.State.DONE
        return True

    def action_accept(self) -> None:
        self.model.accept()

    def action_submit_revision(self) -> None:
        assert (revision_area := self._revision_text_area()) is not None
        self.model.submit_revision(revision_area.text)

    def action_toggle_revision(self) -> None:
        if self.model.state == CommitProposalModel.State.REVIEWING:
            self.model.request_revision()
        elif self.model.state == CommitProposalModel.State.REQUESTING_REVISION:
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

    @on(CommitProposalMenu.Selected)
    def _on_menu_selected(self, event: CommitProposalMenu.Selected) -> None:
        item = event.item
        if item is CommitProposalMenu.Accept:
            self.action_accept()
        elif item is CommitProposalMenu.RequestRevision:
            self.action_toggle_revision()
        elif item is CommitProposalMenu.SubmitRevision:
            self.action_submit_revision()
        elif item is CommitProposalMenu.CancelRevision:
            self.action_toggle_revision()
        elif item is CommitProposalMenu.Reset:
            self.action_reset()
        elif item is CommitProposalMenu.Cancel:
            self.action_cancel()

    # ------------------------------------------------------------------
    # Entry list → details panel
    # ------------------------------------------------------------------

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Remark: row highlighting doesn't change the shared topic, don't need to update it here.
        self._sync_entry_details()

    def _on_entries_changed(self, indices: list[int]) -> None:
        self._sync()

    def _sync(self):
        self._sync_entry_details()
        self._sync_shared_topic_setter()

    def _sync_entry_details(self) -> None:
        entry_list = self._entry_list()
        idx = entry_list.cursor_row

        entry = self.model.entries[idx]
        details = self._entry_details()
        details.set_text(EntryDetails.Areas.Title,   entry.title)
        details.set_text(EntryDetails.Areas.Content, entry.content)

    def _sync_shared_topic_setter(self) -> None:
        shared_topic_setter = self._shared_topic_setter()

        entries = self.model.entries

        if not entries:
            shared_topic_setter.shared_topic = SharedTopicSetter.Sentinel.no_entries
            return

        shared_topic: Topic | None = None
        shared: bool = True
        for entry in entries:
            if shared_topic is None:
                shared_topic = entry.topic

            # Note: a little scared of direct comparison between Topic objects, compare by
            # None-ness/id instead.
            if (
                (entry.topic is None and shared_topic is not None) or
                (entry.topic is not None and shared_topic is None) or
                (entry.topic.id != shared_topic.id)
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
    # Entry details → VM
    # ------------------------------------------------------------------

    @on(ContentEditor.ChangesAccepted)
    def _on_changes_accepted(self, event: ContentEditor.ChangesAccepted) -> None:

        entry_list = self._entry_list()
        idx = entry_list.cursor_row
        
        areas = event.text_areas
        if EntryDetails.Areas.Title in areas:
            self.model.set_entry_title(idx, areas[EntryDetails.Areas.Title].text)
        if EntryDetails.Areas.Content in areas:
            self.model.set_entry_content(idx, areas[EntryDetails.Areas.Content].text)

        details = self._entry_details()
        details.set_text(EntryDetails.Areas.Title,   self.model.entries[idx].title)
        details.set_text(EntryDetails.Areas.Content, self.model.entries[idx].content)

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
            self.query_one("#cp-revision-instructions", RevisionInstructions)
            return
        except Exception:
            pass

        widget = RevisionInstructions(id="cp-revision-instructions")
        self.mount(widget, before=self._menu())
        widget.focus()

    def _unmount_revision_instructions(self) -> None:
        try:
            widget = self.query_one("#cp-revision-instructions", RevisionInstructions)
        except Exception:
            return
        widget.remove()

    def _on_done(self, outcome: CommitProposalModel.Outcome) -> None:
        self._menu().set_state(self.model.state)
        self.set_class(True, "-done")
        self.collapsed = True

        # Populate the DONE-only status surfaces. Both stay in the DOM (CSS gates visibility); the
        # outcome doesn't change after this so we set it once here.
        label = _OUTCOME_LABEL[outcome]
        self.query_one("#cp-done-status",       Static).update(label)
        self.query_one("#cp-done-summary-line", Static).update(self._summary_text(label))

        feedback = (self.model.revision_feedback or "").strip()
        instructions_widget = self.query_one("#cp-done-instructions", Static)
        if feedback:
            instructions_widget.update(feedback)
            instructions_widget.display = True

        # Drop every editable descendant out of the screen-level focus chain. ``_is_node_available``
        # already handles our own alt+arrow graph; this keeps a stale TextArea from soaking up
        # programmatic focus from the chat pane's post-resolve refocus.
        for widget in self.query(Widget):
            if widget.id == "cp-entry-list":
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

            entry_list = self._entry_list()
            idx = entry_list.cursor_row
            if scope == "current":
                self.model.set_entry_topic(idx, topic)
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
        return self.query_one("#cp-shared-topic-setter", SharedTopicSetter)

    def _entry_list(self) -> EntryList:
        return self.query_one("#cp-entry-list", EntryList)

    def _entry_details(self) -> EntryDetails:
        return self.query_one("#cp-entry-details", EntryDetails)

    # ContentEditorMenu height + its margin-top — reserved in the area's height even when the
    # menu is hidden, so the layout doesn't jump when the buffer becomes dirty.
    _DETAILS_MENU_RESERVED = 4

    # Matches the ``max-height`` on ``#cp-entry-list-area`` — keeps the border + hint from
    # marching off-screen when the entry count goes wild (e.g. ``--big``).
    _AREA_MAX_HEIGHT = 25

    def _sync_entry_area_height(self) -> None:
        def _apply() -> None:
            if self.model.is_done:
                return
            try:
                area     = self.query_one("#cp-entry-list-area", Vertical)
                entries  = self._entry_list()
                details  = self._entry_details()
            except Exception:
                return

            natural   = entries.virtual_size.height + 1 + 2
            details_h = details.virtual_size.height + (0 if details.dirty else self._DETAILS_MENU_RESERVED)
            area.styles.height = min(max(natural, details_h), self._AREA_MAX_HEIGHT)

        self.call_after_refresh(_apply)

    def _menu(self) -> CommitProposalMenu:
        return self.query_one("#cp-menu", CommitProposalMenu)

    def _revision_text_area(self) -> RevisionInstructions | None:
        try:
            return self.query_one("#cp-revision-instructions", RevisionInstructions)
        except:
            return None

    def _title_text(self) -> Text:
        n = len(self.model.entries)
        noun = "entry" if n == 1 else "entries"
        text = Text()
        text.append("Commit Proposal", style=f"bold {_TITLE_RED}")
        text.append(f" - ({n} {noun})", style="dim")
        return text

    def _summary_text(self, outcome_label: str) -> str:
        n = len(self.model.entries)
        noun = "entry" if n == 1 else "entries"
        return (
            f"[bold {_TITLE_RED}]Commit Proposal[/]  "
            f"[dim]- ({n} {noun}) -[/]  {outcome_label}"
        )

    def _entry_list_hints_text(self) -> str:
        rows = [
            (Keybind.ProposalCycleType.default_key,     "change type"),
            (Keybind.ProposalSetTopic.default_key,      "set topic"),
            (Keybind.ProposalSetTopicAll.default_key,   "set topic for all"),
            (Keybind.ProposalToggleExclude.default_key, "exclude"),
            ("alt+←↑→↓",                                "navigate"),
        ]
        return "   ".join(f"[#a0a0a0]{k}[/] [#707070]{label}[/]" for k, label in rows)

