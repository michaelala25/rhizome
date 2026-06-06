"""``CommitProposal`` — parent view for ``CommitProposalModel``.

Composes the five focusable regions and orchestrates inter-region focus via a static graph driven
by ``alt+arrow`` bindings. Leaf widgets post ``BoundaryHit`` when plain arrow navigation hits an
edge; the parent looks up the source in the focus graph and forwards focus to the right neighbour.
The parent also owns the topic-picker modal (since the modal needs a ``session_factory`` and the
leaves do not) and the lifecycle keybindings (``ctrl+a/r/c``, ``ctrl+e``, ``shift+t``).

The focus graph (EDITING state)
-------------------------------
Nodes (in declaration order)::

    shared-topic-setter
      ↕ alt+up/down
    entry-list           ←  alt+right →  entry-details-title
      ↕ alt+up/down                       ↕ alt+up/down
    (skip entry-details)                  entry-details-content
                                          ↕ alt+up/down
                                          entry-details-choices (visible only when dirty)
                                          ←  alt+left
                                          → entry-list
    global-choices
      ↕ alt+up/down
    edit-instructions (visible only when ``edit_instructions_visible``)

``focus_first`` lands on ``entry-list`` per the spec.

DONE state
----------
On the EDITING→DONE transition the view flips its own ``_collapsed`` flag to True, blurs whatever
descendant currently has focus, and tightens the focus graph so only ``cp-entry-list`` remains
available (every other node returns False from ``_is_node_available``). The TextAreas in the
details panel and the edit-instructions area are flipped to ``can_focus = False`` so they leave the
global focus chain too; the entry-list keeps its cursor so the user can still browse the proposal.

The upper-right ▶/▼ button toggles ``_collapsed``; ``enter`` on the widget itself (or on the
still-focusable entry list) does the same. When collapsed, the editing content is hidden and a
centered summary block renders the title, count, the final-state label (approved / edits requested
/ cancelled), and — if the user supplied edit-instructions before approving — a dim grey readout of
that buffer.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Static

from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel
from rhizome.tui.widgets.commit_proposal.choices import CommitProposalChoices
from rhizome.tui.widgets.commit_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.commit_proposal.entry_details import EntryDetails
from rhizome.tui.widgets.commit_proposal.entry_list import EntryList
from rhizome.tui.widgets.commit_proposal.messages import SetTopicRequested
from rhizome.tui.widgets.commit_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind


# Final-state colours shown in the collapsed DONE summary header. Matched against the trio used in
# the flashcard-review widget so the chat-pane vocabulary stays consistent across surfaces.
_APPROVED_GREEN = "rgb(120,210,110)"
_EDITS_YELLOW = "rgb(235,180,90)"
_CANCEL_RED = "rgb(235,100,100)"


class CommitProposal(NavigableFeedItemViewBase[CommitProposalModel], FocusOrchestrationMixin):
    """Parent view for the commit-proposal interrupt."""

    DEFAULT_CSS = """
    CommitProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        max-height: 35;
        padding: 0;
    }
    CommitProposal #cp-top-row {
        height: 1;
        background: transparent;
    }
    CommitProposal #cp-title {
        height: 1;
        width: 1fr;
        padding: 0 1;
        background: transparent;
    }
    CommitProposal #cp-description {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: #707070;
    }
    CommitProposal #cp-description-spacer {
        height: 1;
    }
    CommitProposal #cp-shared-topic-setter {
        margin-bottom: 1;
    }
    CommitProposal #cp-entry-list-area {
        width: 3fr;
        height: auto;
        layout: vertical;
        border: solid #3a3a3a;
        border-title-align: left;
        border-title-color: rgb(120,120,120);
    }
    CommitProposal #cp-entry-list-area:focus-within {
        border: solid #8a8a8a;
    }
    CommitProposal #cp-entry-list {
        min-height: 9;
    }
    CommitProposal #cp-entry-list-hints {
        dock: bottom;
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }
    /* Bump specificity above EntryDetails' own type selector so the inherited
       Vertical { height: 1fr } default can't win same-specificity ties — under certain
       mount orderings Textual's stylesheet resolution flips and 1fr wins, which stretches
       EntryDetails to fill cp-middle and feeds a runaway loop in _sync_entry_area_height. */
    CommitProposal #cp-entry-details {
        height: auto;
    }
    CommitProposal #cp-middle {
        height: auto;
        padding: 0 1;
    }
    CommitProposal #cp-global-choices {
        margin: 1 0;
    }
    CommitProposal #cp-collapse {
        dock: right;
        width: 3;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: rgb(120,120,120);
        display: none;
    }
    CommitProposal #cp-collapse:hover {
        color: rgb(220,220,220);
    }
    CommitProposal #cp-done-summary-line {
        height: auto;
        width: 1fr;
        padding: 1 2;
        text-align: center;
        display: none;
    }
    CommitProposal #cp-done-status {
        height: auto;
        width: 1fr;
        margin: 1 0;
        text-align: center;
        display: none;
    }
    CommitProposal #cp-done-instructions {
        margin: 1 4 0 4;
        padding: 1 2;
        height: auto;
        background: rgb(40,40,40);
        color: rgb(180,180,180);
        display: none;
    }
    """

    BINDINGS = [
        # alt+arrows: always-on focus orchestration (works from any focused descendant).
        Keybind.FocusUp   .as_binding("focus_neighbour('up')",    show=False),
        Keybind.FocusDown .as_binding("focus_neighbour('down')",  show=False),
        Keybind.FocusLeft .as_binding("focus_neighbour('left')",  show=False),
        Keybind.FocusRight.as_binding("focus_neighbour('right')", show=False),
        # Plain up/down catch-all: leaves either don't bind these (SharedTopicSetter) or report
        # them as inactive via ``check_action`` when at a boundary (EntryList) / always inactive
        # (CommitProposalChoices). Either way the key bubbles here.
        Keybind.CursorUp  .as_binding("navigate_cursor('up')",   show=False),
        Keybind.CursorDown.as_binding("navigate_cursor('down')", show=False),
        # ``e`` from anywhere that doesn't consume it (i.e. EntryList) jumps focus into the
        # details panel, equivalent to alt+right from the entry list.
        Keybind.ProposalEdit.as_binding("focus_neighbour('right')", show=False),
        Keybind.ProposalAcceptAll.             as_binding("accept_all",                show=False),
        Keybind.ProposalReset.                 as_binding("reset",                     show=False),
        Keybind.ProposalCancel.                as_binding("cancel",                    show=False),
        Keybind.ProposalToggleEditInstructions.as_binding("toggle_edit_instructions", show=False),
        Keybind.ProposalSetTopicAll.           as_binding("set_topic_all",             show=False),
        # DONE-state collapse toggle. Active only when state == DONE (gated in ``check_action``)
        # so the editing-state ``enter`` consumers (SharedTopicSetter, the EntryDetails
        # ConfirmableTextAreas, CommitProposalChoices) keep their semantics.
        Keybind.ProposalToggleCollapsed.as_binding("toggle_collapsed", show=False),
    ]

    # Static focus graph. The fallback list on ``cp-details-content``'s ``down`` skips
    # ``cp-details-choices`` when the per-entry edit isn't dirty (that node is gated by
    # ``_is_node_available`` below), landing on ``cp-global-choices`` instead.
    FOCUS_GRAPH = FocusGraph(
        source="cp-entry-list",
        edges={
            "cp-shared-topic-setter": {"down": "cp-entry-list"},
            "cp-entry-list": {
                "up":    "cp-shared-topic-setter",
                "down":  "cp-global-choices",
                "right": "cp-details-title",
            },
            "cp-details-title": {
                "up":   "cp-shared-topic-setter",
                "down": "cp-details-content",
                "left": "cp-entry-list",
            },
            "cp-details-content": {
                "up":   "cp-details-title",
                "down": ["cp-details-choices", "cp-global-choices"],
                "left": "cp-entry-list",
            },
            "cp-details-choices": {
                "up":   "cp-details-content",
                "down": "cp-global-choices",
                "left": "cp-entry-list",
            },
            "cp-global-choices": {
                "up":   "cp-entry-list",
                "down": "cp-edit-instructions",
            },
            "cp-edit-instructions": {
                "up": "cp-global-choices",
            },
        },
    )

    def __init__(self, vm: CommitProposalModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        # Carried on the VM. Used by the topic-picker modal; ``None`` disables it — useful for unit
        # tests and the ``/test-commit-proposal`` slash command which doesn't need real topic data.
        self._session_factory = vm.session_factory

        # DONE-state display flag, owned entirely by the view. Defaults to False; the EDITING→DONE
        # transition in ``_refresh`` flips it to True on first observation (and never again from
        # the VM side — only ``action_toggle_collapsed`` and the ▶/▼ button move it after that).
        self._collapsed: bool = False
        # Edge-detection flag for the EDITING→DONE transition. ``_refresh`` flips this True the
        # first time state == DONE so the one-shot collapse + blur + can_focus updates run exactly
        # once.
        self._entered_done: bool = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        # Upper-right ▶/▼ button shares the title row. Hidden in EDITING; toggled visible by
        # ``_refresh_done_surface`` once the VM reaches DONE. Intentionally outside the focus
        # graph — the parent's ``enter`` binding handles keyboard toggling when focus is on the
        # entry list (or on the widget itself).
        with Horizontal(id="cp-top-row"):
            yield Static(self._title_text(), id="cp-title")
            yield Button("▼", id="cp-collapse")
        yield Static(
            "The agent has drafted these knowledge entries to commit — edit them inline, "
            "exclude any you don't want, or request revisions before approving.",
            id="cp-description",
        )
        yield Static("", id="cp-description-spacer")
        yield SharedTopicSetter(self._vm, id="cp-shared-topic-setter")
        with Horizontal(id="cp-middle"):
            entry_area = Vertical(id="cp-entry-list-area")
            entry_area.border_title = "Entries"
            with entry_area:
                yield EntryList(self._vm, id="cp-entry-list")
                yield Static(self._entry_list_hints_text(), id="cp-entry-list-hints")
            yield EntryDetails(self._vm.details, id="cp-entry-details")
        yield CommitProposalChoices(self._vm, id="cp-global-choices")
        # DONE-expanded status label — fills the slot the choices vacated, rendering the colored
        # final-state word (approved / edits requested / cancelled).
        yield Static("", id="cp-done-status")
        yield EditInstructionsArea(self._vm, id="cp-edit-instructions")
        # Read-only edit-instructions readout shown in DONE (any). Borderless dim-grey block —
        # same formatting in DONE-expanded and DONE-collapsed.
        yield Static("", id="cp-done-instructions")
        # Collapsed-DONE centered summary line ("Commit Proposal - (N entries) - <state>").
        yield Static("", id="cp-done-summary-line")

    def _entry_list_hints_text(self) -> str:
        # Keys sourced from the Keybind concepts; the labels are this row's own copy.
        rows = [
            (Keybind.ProposalCycleType.default_key,     "change type"),
            (Keybind.ProposalSetTopic.default_key,      "set topic"),
            (Keybind.ProposalSetTopicAll.default_key,   "set topic for all"),
            (Keybind.ProposalToggleExclude.default_key, "exclude"),
            ("alt+←↑→↓", "navigate"),
        ]
        return "   ".join(f"[#a0a0a0]{k}[/] [#707070]{label}[/]" for k, label in rows)

    def _title_text(self) -> Text:
        # Entry count is fixed at construction (proposals can't grow or shrink), so the title is
        # rendered once at compose time and never refreshed.
        n = len(self._vm.entries)
        noun = "entry" if n == 1 else "entries"
        text = Text()
        text.append("Commit Proposal", style="bold rgb(255,80,80)")
        text.append(f" - ({n} {noun})", style="dim")
        return text

    def on_mount(self) -> None:
        # ``ViewBase`` wires the vm.dirty → _refresh subscription. The CommitProposal parent itself
        # owns no rendered surface (children handle their own paint), so ``_refresh`` is a no-op.
        # We land focus on the EntryList per the spec, but only in EDITING — a re-mount after the
        # interrupt resolves shouldn't steal focus from the chat input.
        if self._vm.state == CommitProposalModel.State.EDITING:
            self.focus_first()
        # Drives the programmatic entry-area↔details height pin. ``vm.details.dirty`` fires when
        # the focused entry changes or its content edits land — both can shift the details panel's
        # rendered height, which is what we're tracking.
        self._vm.details.subscribe(self._vm.details.dirty, self._sync_entry_area_height)

    def on_unmount(self) -> None:
        self._vm.details.unsubscribe(self._vm.details.dirty, self._sync_entry_area_height)

    def on_resize(self) -> None:
        self._sync_entry_area_height()

    def focus_first(self) -> str | None:
        # DONE-collapsed: the parent itself stays focused so ``enter`` toggles re-expand. Skipping
        # the inward delegation is enough — the mixin's ``on_focus`` calls this and respects the
        # ``None`` return.
        if self._vm.state == CommitProposalModel.State.DONE and self._collapsed:
            return None
        return super().focus_first()

    def _sync_entry_area_height(self) -> None:
        # Pin entry-list-area's height to ``max(its own natural content height, details height)``
        # so a tall EntryDetails doesn't leave dead vertical space between the bordered table and
        # the CommitProposalChoices row below. Runs after layout settles so children sizes are real.
        #
        # Both sides read ``virtual_size``, not ``size``: ``area``'s rendered size tracks whatever
        # we just wrote to ``area.styles.height`` (the entry list fills the area), and ``details``'
        # rendered size tracks ``cp-middle``'s height (the Horizontal stretches siblings), so
        # ``size`` on either widget would let this function read its own previous output. Skipping
        # while DONE-collapsed because the entire ``cp-middle`` subtree is ``display: none``.
        def _apply() -> None:
            if self._vm.state == CommitProposalModel.State.DONE and self._collapsed:
                return
            try:
                area = self.query_one("#cp-entry-list-area", Vertical)
                details = self.query_one("#cp-entry-details", EntryDetails)
                entry_list = self.query_one("#cp-entry-list", EntryList)
            except Exception:
                return
            natural = entry_list.virtual_size.height + 1 + 2
            target = max(natural, details.virtual_size.height)
            area.styles.height = target
        self.call_after_refresh(_apply)

    def _refresh(self) -> None:
        # Bulk of the editing-state content is rendered by child widgets that subscribe to
        # vm.dirty independently — the parent's job here is the DONE-state surface (collapsed/
        # expanded gating, summary block, button) and the one-shot EDITING→DONE edge.
        if self._vm.state == CommitProposalModel.State.DONE and not self._entered_done:
            self._handle_done_transition()
            self._entered_done = True

        self._refresh_done_surface()

    def _refresh_done_surface(self) -> None:
        """Sync button visibility, the DONE status widgets, and the visibility of the editing
        content based on (state, collapsed). Idempotent — runs on every dirty emit and on every
        toggle, but only mutates widgets when their target state has changed.

        Visibility matrix (rows are widgets, columns are EDITING / DONE-expanded / DONE-collapsed)::

            cp-title, description, spacer, topic-setter, middle  →  ●  ●  ○
            cp-global-choices                                    →  ●  ○  ○
            cp-done-status (centered final-state label)          →  ○  ●  ○
            cp-edit-instructions (TextArea)                      →  ●*  ○  ○      (* vm flag)
            cp-done-instructions (dim grey readout)              →  ○  ●†  ●†     († non-empty)
            cp-done-summary-line (centered title + state)        →  ○  ○  ●
        """
        in_done = self._vm.state == CommitProposalModel.State.DONE
        collapsed_done = in_done and self._collapsed
        expanded_done = in_done and not self._collapsed

        # ▶/▼ button — shown in DONE only; arrow flips with collapsed state.
        btn = self.query_one("#cp-collapse", Button)
        btn.display = in_done
        btn.label = "▶" if collapsed_done else "▼"

        # ``cp-top-row`` stays visible across all three modes (its only visible occupant in
        # collapsed-DONE is the dock-right button) — the inner title Static gets hidden when
        # collapsed so it doesn't duplicate the centered summary header below.
        editing_or_expanded_done = not collapsed_done
        for wid in (
            "cp-title", "cp-description", "cp-description-spacer",
            "cp-shared-topic-setter", "cp-middle",
        ):
            self.query_one(f"#{wid}", Widget).display = editing_or_expanded_done

        # Choices live only in EDITING; in DONE-expanded their slot is occupied by the centered
        # status label instead, and DONE-collapsed hides both.
        self.query_one("#cp-global-choices", Widget).display = not in_done
        self.query_one("#cp-done-status", Widget).display = expanded_done

        # Edit-instructions area: only ever the editable TextArea while editing AND the VM flag
        # is set; in DONE the read-only readout block takes over (and the TextArea is hidden).
        self.query_one("#cp-edit-instructions", Widget).display = (
            not in_done and self._vm.edit_instructions_visible
        )

        # Done-state read-only readout — same dim-grey formatting in expanded and collapsed.
        instructions = self._vm.edit_instructions.strip()
        instructions_widget = self.query_one("#cp-done-instructions", Static)
        instructions_widget.display = in_done and bool(instructions)
        if instructions_widget.display:
            instructions_widget.update(instructions)

        # Collapsed-DONE centered summary line.
        summary_line = self.query_one("#cp-done-summary-line", Static)
        summary_line.display = collapsed_done

        if in_done:
            state_label = self._done_state_label()
            if expanded_done:
                self.query_one("#cp-done-status", Static).update(state_label)
            if collapsed_done:
                n = len(self._vm.entries)
                noun = "entry" if n == 1 else "entries"
                summary_line.update(
                    f"[bold rgb(255,80,80)]Commit Proposal[/]  "
                    f"[dim]- ({n} {noun}) -[/]  {state_label}"
                )

    def _done_state_label(self) -> str:
        """Markup-formatted colored state word shared by the collapsed summary line and the
        DONE-expanded status slot."""
        if self._vm.cancelled:
            return f"[bold {_CANCEL_RED}]cancelled[/]"
        if self._vm.edit_instructions.strip():
            return f"[bold {_EDITS_YELLOW}]edits requested[/]"
        return f"[bold {_APPROVED_GREEN}]approved[/]"

    def _handle_done_transition(self) -> None:
        """One-shot wiring run the first time we observe state == DONE.

        Flips the view's collapsed flag to True (the default landing surface for a freshly-done
        proposal), drops every editable descendant out of the focus chain, and blurs whatever
        currently holds focus so the chat input can take over once the interrupt resolves."""
        self._collapsed = True

        # Tear down the editable surface. Beyond removing each node from the parent's focus graph
        # (handled by ``_is_node_available``), each widget gets ``can_focus = False`` so it
        # also leaves the screen-level tab chain — important because the interrupt will resolve
        # imminently and we don't want a stale TextArea to soak up programmatic focus from the
        # chat pane's post-resolve refocus.
        for wid in (
            "cp-shared-topic-setter",
            "cp-details-title",
            "cp-details-content",
            "cp-details-choices",
            "cp-global-choices",
            "cp-edit-instructions",
        ):
            try:
                self.query_one(f"#{wid}", Widget).can_focus = False
            except Exception:
                pass

        # Blur the currently-focused descendant. The chat pane typically refocuses its input on
        # interrupt resolution, so this is mostly a defensive nudge — keeps a now-non-focusable
        # TextArea from holding focus until the chat pane gets around to it.
        screen = self.screen
        if screen is None:
            return
        focused = screen.focused
        if focused is None:
            return
        node: Widget | None = focused
        while node is not None and node is not self:
            node = node.parent
        if node is self:
            focused.blur()

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        """Hard refocus along the focus graph (``alt+arrow``). Leaves target cursors untouched."""
        self.focus_neighbour(direction)  # type: ignore[arg-type]

    def action_navigate_cursor(self, direction: str) -> None:
        """Plain ``up``/``down`` cross-region jump — refocuses and resets the target's cursor to
        the natural entry side (top when arriving from above, bottom from below)."""
        target_id = self.focus_neighbour(direction)  # type: ignore[arg-type]
        if target_id is None:
            return
        self._reset_target_cursor_for_continuation(target_id, direction)

    def _reset_target_cursor_for_continuation(self, target_id: str, direction: str) -> None:
        from rhizome.tui.widgets.shared.choices_list import ChoiceList

        if target_id == "cp-entry-list":
            n = len(self._vm.entries)
            if n == 0:
                return
            self._vm.set_cursor(0 if direction == "down" else n - 1)
            return

        if target_id in ("cp-global-choices", "cp-details-choices"):
            widget = self.query_one(f"#{target_id}", ChoiceList)
            n = len(widget.choices())
            widget._cursor = 0 if direction == "down" else max(0, n - 1)
            widget._refresh()

    def _is_node_available(self, node_id: str) -> bool:
        # In DONE only the entry-list stays in the graph — the user browses the proposal but can't
        # edit anything. Alt+arrow traversals from the entry list dead-end at every neighbour.
        if self._vm.state == CommitProposalModel.State.DONE:
            return node_id == "cp-entry-list"
        if node_id == "cp-details-choices":
            return self._vm.details.is_dirty
        if node_id == "cp-edit-instructions":
            return self._vm.edit_instructions_visible
        return True

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_accept_all(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.accept_all()

    def action_reset(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.reset()

    def action_cancel(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.cancel()

    def action_toggle_edit_instructions(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.toggle_edit_instructions_area()
        if self._vm.edit_instructions_visible:
            try:
                self.query_one("#cp-edit-instructions", Widget).focus()
            except Exception:
                pass

    def action_set_topic_all(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._open_topic_picker(scope="all")

    def action_toggle_collapsed(self) -> None:
        """DONE-only enter binding. No-ops in EDITING so it can't fight with the EDITING-state
        ``enter`` consumers (the SharedTopicSetter's set-topic-all binding, the
        ConfirmableTextArea accept hook, the CommitProposalChoices in-list activator)."""
        if self._vm.state != CommitProposalModel.State.DONE:
            return
        self._toggle_collapsed()

    @on(Button.Pressed)
    def _on_collapse_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "cp-collapse":
            return
        event.stop()
        if self._vm.state != CommitProposalModel.State.DONE:
            return
        self._toggle_collapsed()

    def _toggle_collapsed(self) -> None:
        # Flip _collapsed, then swap focus so the enter binding lives on whichever node makes sense
        # for the new state: parent self when collapsed (so enter re-expands), entry list when
        # expanded (so the user can browse the proposal).
        self._collapsed = not self._collapsed
        self._refresh_done_surface()
        if self._collapsed:
            self.focus()
        else:
            try:
                self.query_one("#cp-entry-list", EntryList).focus()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Topic picker
    # ------------------------------------------------------------------

    @on(SetTopicRequested)
    def _on_set_topic_requested(self, event: SetTopicRequested) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._open_topic_picker(scope=event.scope)
        event.stop()

    def _open_topic_picker(self, *, scope: str) -> None:
        if self._session_factory is None:
            # No session → no modal. Quietly no-op so the rest of the widget remains usable in
            # test contexts that don't supply a session_factory.
            return

        # Deferred import: ``TopicSelectorScreen`` pulls ``rhizome.tui.widgets.TopicTree``, which
        # is part of the widgets package init — importing at module load creates a cycle.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        def _on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                return
            topic_id, topic_name = result
            if scope == "current":
                self._vm.set_current_entry_topic(topic_id, topic_name)
            elif scope == "all":
                self._vm.set_topic_all(topic_id, topic_name)

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._session_factory),
            _on_dismiss,
        )
