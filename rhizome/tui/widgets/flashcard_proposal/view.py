"""``FlashcardProposal`` — parent view for ``FlashcardProposalModel``.

Composes the five focusable regions and orchestrates inter-region focus via a static graph driven
by ``alt+arrow`` bindings. Leaves whose plain-arrow bindings are inactive at a boundary (via
``check_action``) — or that simply don't bind the arrow keys — let the keystroke flow up to this
view's own arrow handlers, which forward focus along the graph. The parent also owns the
topic-picker modal (since the modal needs a ``session_factory`` and the leaves do not) and the
lifecycle keybindings (``ctrl+a/r/c``, ``ctrl+e``, ``shift+t``).

The focus graph (EDITING state)
-------------------------------
Nodes (in declaration order)::

    shared-topic-setter
      ↕ alt+up/down
    flashcard-list             ←  alt+right →  flashcard-details-question
      ↕ alt+up/down                              ↕ alt+up/down
    (skip flashcard-details)                    flashcard-details-answer
                                                 ↕ alt+up/down
                                                flashcard-details-testing-notes
                                                 ↕ alt+up/down
                                                flashcard-details-choices (visible only when dirty)
                                                 ←  alt+left
                                                 → flashcard-list
    global-choices
      ↕ alt+up/down
    edit-instructions (visible only when ``edit_instructions_visible``)

The read-only ``fp-details-linked-entries`` Static is rendered between testing-notes and the
details choices but is intentionally absent from the focus graph — it isn't interactive.

``focus_first`` lands on ``flashcard-list`` per the same convention as commit-proposal.

DONE state
----------
Mirrors the commit-proposal view: on EDITING→DONE the view flips its own ``_collapsed`` to True,
blurs the focused descendant, drops every editable node out of the focus graph (only
``fp-flashcard-list`` stays available), and sets ``can_focus = False`` on the SharedTopicSetter,
the three details TextAreas, both ChoiceLists, and the edit-instructions area. The upper-right
▶/▼ button toggles ``_collapsed``; ``enter`` does the same (gated to DONE-only inside
``action_toggle_collapsed``). Collapsed renders a centered "Flashcard Proposal - (N flashcards) -
<final state>" summary plus the dim grey edit-instructions readout when non-empty.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Static

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalModel
from rhizome.tui.widgets.flashcard_proposal.choices import FlashcardProposalChoices
from rhizome.tui.widgets.flashcard_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.flashcard_proposal.flashcard_details import FlashcardDetails
from rhizome.tui.widgets.flashcard_proposal.flashcard_list import FlashcardList
from rhizome.tui.widgets.flashcard_proposal.messages import SetTopicRequested
from rhizome.tui.widgets.flashcard_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind


# Final-state colours shown in the collapsed DONE summary header. Matched against the commit-
# proposal trio so the chat-pane vocabulary stays consistent across both surfaces.
_APPROVED_GREEN = "rgb(120,210,110)"
_EDITS_YELLOW = "rgb(235,180,90)"
_CANCEL_RED = "rgb(235,100,100)"


class FlashcardProposal(NavigableFeedItemViewBase[FlashcardProposalModel], FocusOrchestrationMixin):
    """Parent view for the flashcard-proposal interrupt."""

    DEFAULT_CSS = """
    FlashcardProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        max-height: 40;
        padding: 0;
    }
    FlashcardProposal #fp-top-row {
        height: 1;
        background: transparent;
    }
    FlashcardProposal #fp-title {
        height: 1;
        width: 1fr;
        padding: 0 1;
        background: transparent;
    }
    FlashcardProposal #fp-description {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: #707070;
    }
    FlashcardProposal #fp-description-spacer {
        height: 1;
    }
    FlashcardProposal #fp-shared-topic-setter {
        margin-bottom: 1;
    }
    FlashcardProposal #fp-flashcard-list-area {
        width: 3fr;
        height: auto;
        layout: vertical;
        border: solid #3a3a3a;
        border-title-align: left;
        border-title-color: rgb(120,120,120);
    }
    FlashcardProposal #fp-flashcard-list-area:focus-within {
        border: solid #8a8a8a;
    }
    FlashcardProposal #fp-flashcard-list {
        min-height: 9;
    }
    FlashcardProposal #fp-flashcard-list-hints {
        dock: bottom;
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }
    /* Bump specificity above FlashcardDetails' own type selector so the inherited
       Vertical { height: 1fr } default can't win same-specificity ties — under certain
       mount orderings Textual's stylesheet resolution flips and 1fr wins, which stretches
       FlashcardDetails to fill fp-middle and feeds a runaway loop in _sync_list_area_height. */
    FlashcardProposal #fp-flashcard-details {
        height: auto;
    }
    FlashcardProposal #fp-middle {
        height: auto;
        padding: 0 1;
    }
    FlashcardProposal #fp-global-choices {
        margin: 1 0;
    }
    FlashcardProposal #fp-collapse {
        dock: right;
        width: 3;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: rgb(120,120,120);
        display: none;
    }
    FlashcardProposal #fp-collapse:hover {
        color: rgb(220,220,220);
    }
    FlashcardProposal #fp-done-summary-line {
        height: auto;
        width: 1fr;
        padding: 1 2;
        text-align: center;
        display: none;
    }
    FlashcardProposal #fp-done-status {
        height: auto;
        width: 1fr;
        margin: 1 0;
        text-align: center;
        display: none;
    }
    FlashcardProposal #fp-done-instructions {
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
        # them as inactive via ``check_action`` when at a boundary (FlashcardList,
        # FlashcardProposalChoices). Either way the key bubbles here.
        Keybind.CursorUp  .as_binding("navigate_cursor('up')",   show=False),
        Keybind.CursorDown.as_binding("navigate_cursor('down')", show=False),
        # ``e`` from anywhere that doesn't consume it (i.e. FlashcardList) jumps focus into the
        # details panel, equivalent to alt+right from the flashcard list.
        Keybind.ProposalEdit.as_binding("focus_neighbour('right')", show=False),
        Keybind.ProposalAcceptAll.             as_binding("accept_all",                show=False),
        Keybind.ProposalReset.                 as_binding("reset",                     show=False),
        Keybind.ProposalCancel.                as_binding("cancel",                    show=False),
        Keybind.ProposalToggleEditInstructions.as_binding("toggle_edit_instructions", show=False),
        Keybind.ProposalSetTopicAll.           as_binding("set_topic_all",             show=False),
        # DONE-state collapse toggle — same dual-meaning ``enter`` strategy as commit-proposal.
        # The action no-ops in EDITING so it doesn't fight with SharedTopicSetter / TextArea /
        # ChoiceList enter consumers there.
        Keybind.ProposalToggleCollapsed.as_binding("toggle_collapsed", show=False),
    ]

    # Static focus graph. The fallback list on ``fp-details-testing-notes``'s ``down`` skips
    # ``fp-details-choices`` when the per-flashcard edit isn't dirty (gated by
    # ``_is_node_available`` below), landing on ``fp-global-choices`` instead.
    FOCUS_GRAPH = FocusGraph(
        source="fp-flashcard-list",
        edges={
            "fp-shared-topic-setter": {"down": "fp-flashcard-list"},
            "fp-flashcard-list": {
                "up":    "fp-shared-topic-setter",
                "down":  "fp-global-choices",
                "right": "fp-details-question",
            },
            "fp-details-question": {
                "up":   "fp-shared-topic-setter",
                "down": "fp-details-answer",
                "left": "fp-flashcard-list",
            },
            "fp-details-answer": {
                "up":   "fp-details-question",
                "down": "fp-details-testing-notes",
                "left": "fp-flashcard-list",
            },
            "fp-details-testing-notes": {
                "up":   "fp-details-answer",
                "down": ["fp-details-choices", "fp-global-choices"],
                "left": "fp-flashcard-list",
            },
            "fp-details-choices": {
                "up":   "fp-details-testing-notes",
                "down": "fp-global-choices",
                "left": "fp-flashcard-list",
            },
            "fp-global-choices": {
                "up":   "fp-flashcard-list",
                "down": "fp-edit-instructions",
            },
            "fp-edit-instructions": {
                "up": "fp-global-choices",
            },
        },
    )

    def __init__(self, vm: FlashcardProposalModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        # Carried on the VM. Used by the topic-picker modal; ``None`` disables it — useful for unit
        # tests and the ``/test-flashcard-proposal`` slash command which doesn't need real topic data.
        self._session_factory = vm.session_factory

        # DONE-state display flag, owned entirely by the view. Defaults to False; the EDITING→DONE
        # transition in ``_refresh`` flips it to True on first observation.
        self._collapsed: bool = False
        # Edge-detection flag for the EDITING→DONE transition.
        self._entered_done: bool = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        # Upper-right ▶/▼ button shares the title row. Hidden in EDITING; toggled visible by
        # ``_refresh_done_surface`` once the VM reaches DONE.
        with Horizontal(id="fp-top-row"):
            yield Static(self._title_text(), id="fp-title")
            yield Button("▼", id="fp-collapse")

        yield Static(
            "The agent has drafted these flashcards to commit — edit them inline, exclude any "
            "you don't want, or request revisions before approving.",
            id="fp-description",
        )
        yield Static("", id="fp-description-spacer")
        yield SharedTopicSetter(self._vm, id="fp-shared-topic-setter")
        with Horizontal(id="fp-middle"):
            list_area = Vertical(id="fp-flashcard-list-area")
            list_area.border_title = "Flashcards"
            with list_area:
                yield FlashcardList(self._vm, id="fp-flashcard-list")
                yield Static(self._flashcard_list_hints_text(), id="fp-flashcard-list-hints")
            yield FlashcardDetails(self._vm.details, id="fp-flashcard-details")
        yield FlashcardProposalChoices(self._vm, id="fp-global-choices")
        # DONE-expanded status label — fills the slot the choices vacated, rendering the colored
        # final-state word (approved / edits requested / cancelled).
        yield Static("", id="fp-done-status")
        yield EditInstructionsArea(self._vm, id="fp-edit-instructions")
        # Read-only edit-instructions readout shown in DONE (any). Borderless dim-grey block —
        # same formatting in DONE-expanded and DONE-collapsed.
        yield Static("", id="fp-done-instructions")
        # Collapsed-DONE centered summary line.
        yield Static("", id="fp-done-summary-line")

    def _flashcard_list_hints_text(self) -> str:
        rows = [
            (Keybind.ProposalSetTopic.default_key,      "set topic"),
            (Keybind.ProposalSetTopicAll.default_key,   "set topic for all"),
            (Keybind.ProposalToggleExclude.default_key, "exclude"),
            ("alt+←↑→↓", "navigate"),
        ]
        return "   ".join(f"[#a0a0a0]{k}[/] [#707070]{label}[/]" for k, label in rows)

    def _title_text(self) -> Text:
        # Flashcard count is fixed at construction (proposals can't grow or shrink), so the title
        # is rendered once at compose time and never refreshed.
        n = len(self._vm.flashcards)
        noun = "flashcard" if n == 1 else "flashcards"
        text = Text()
        text.append("Flashcard Proposal", style="bold rgb(255,80,80)")
        text.append(f" - ({n} {noun})", style="dim")
        return text

    def on_mount(self) -> None:
        # ``ViewBase`` wires the vm.dirty → _refresh subscription. The FlashcardProposal parent
        # itself owns no rendered surface (children handle their own paint), so ``_refresh`` is a
        # no-op. We land focus on the FlashcardList per the spec, but only in EDITING — a re-mount
        # after the interrupt resolves shouldn't steal focus from the chat input.
        if self._vm.state == FlashcardProposalModel.State.EDITING:
            self.focus_first()
        # Drives the programmatic flashcard-area↔details height pin. ``vm.details.dirty`` fires
        # when the focused flashcard changes or its content edits land — both can shift the
        # details panel's rendered height, which is what we're tracking.
        self._vm.details.subscribe(self._vm.details.Callbacks.OnDirty, self._sync_list_area_height)

    def on_unmount(self) -> None:
        self._vm.details.unsubscribe(self._vm.details.Callbacks.OnDirty, self._sync_list_area_height)

    def on_resize(self) -> None:
        self._sync_list_area_height()

    def focus_first(self) -> str | None:
        # DONE-collapsed: the parent itself stays focused so ``enter`` toggles re-expand. Skipping
        # the inward delegation is enough — the mixin's ``on_focus`` calls this and respects the
        # ``None`` return.
        if self._vm.state == FlashcardProposalModel.State.DONE and self._collapsed:
            return None
        return super().focus_first()

    def _sync_list_area_height(self) -> None:
        # Pin flashcard-list-area's height to ``max(its own natural content height, details
        # height)`` so a tall FlashcardDetails doesn't leave dead vertical space between the
        # bordered table and the FlashcardProposalChoices row below.
        #
        # Both sides read ``virtual_size``, not ``size``: ``area``'s rendered size tracks whatever
        # we just wrote to ``area.styles.height`` (the flashcard list fills the area), and
        # ``details``' rendered size tracks ``fp-middle``'s height (the Horizontal stretches
        # siblings), so ``size`` on either widget would let this function read its own previous
        # output. Skipping while DONE-collapsed because the entire ``fp-middle`` subtree is
        # ``display: none``.
        def _apply() -> None:
            if self._vm.state == FlashcardProposalModel.State.DONE and self._collapsed:
                return
            try:
                area = self.query_one("#fp-flashcard-list-area", Vertical)
                details = self.query_one("#fp-flashcard-details", Widget)
                flashcard_list = self.query_one("#fp-flashcard-list", FlashcardList)
            except Exception:
                return
            natural = flashcard_list.virtual_size.height + 1 + 2
            target = max(natural, details.virtual_size.height)
            area.styles.height = target
        self.call_after_refresh(_apply)

    def _refresh(self) -> None:
        # Bulk of the editing-state content is rendered by child widgets that subscribe to
        # vm.dirty independently — the parent's job here is the DONE-state surface (collapsed/
        # expanded gating, summary block, button) and the one-shot EDITING→DONE edge.
        if self._vm.state == FlashcardProposalModel.State.DONE and not self._entered_done:
            self._handle_done_transition()
            self._entered_done = True

        self._refresh_done_surface()

    def _refresh_done_surface(self) -> None:
        """Sync button visibility, the DONE status widgets, and the visibility of the editing
        content based on (state, collapsed). Idempotent.

        Visibility matrix (rows are widgets, columns are EDITING / DONE-expanded / DONE-collapsed)::

            fp-title, description, spacer, topic-setter, middle  →  ●  ●  ○
            fp-global-choices                                    →  ●  ○  ○
            fp-done-status (centered final-state label)          →  ○  ●  ○
            fp-edit-instructions (TextArea)                      →  ●*  ○  ○      (* vm flag)
            fp-done-instructions (dim grey readout)              →  ○  ●†  ●†     († non-empty)
            fp-done-summary-line (centered title + state)        →  ○  ○  ●
        """
        in_done = self._vm.state == FlashcardProposalModel.State.DONE
        collapsed_done = in_done and self._collapsed
        expanded_done = in_done and not self._collapsed

        btn = self.query_one("#fp-collapse", Button)
        btn.display = in_done
        btn.label = "▶" if collapsed_done else "▼"

        editing_or_expanded_done = not collapsed_done
        for wid in (
            "fp-title", "fp-description", "fp-description-spacer",
            "fp-shared-topic-setter", "fp-middle",
        ):
            self.query_one(f"#{wid}", Widget).display = editing_or_expanded_done

        # Choices live only in EDITING; DONE-expanded swaps in the centered status label.
        self.query_one("#fp-global-choices", Widget).display = not in_done
        self.query_one("#fp-done-status", Widget).display = expanded_done

        self.query_one("#fp-edit-instructions", Widget).display = (
            not in_done and self._vm.edit_instructions_visible
        )

        instructions = self._vm.edit_instructions.strip()
        instructions_widget = self.query_one("#fp-done-instructions", Static)
        instructions_widget.display = in_done and bool(instructions)
        if instructions_widget.display:
            instructions_widget.update(instructions)

        summary_line = self.query_one("#fp-done-summary-line", Static)
        summary_line.display = collapsed_done

        if in_done:
            state_label = self._done_state_label()
            if expanded_done:
                self.query_one("#fp-done-status", Static).update(state_label)
            if collapsed_done:
                n = len(self._vm.flashcards)
                noun = "flashcard" if n == 1 else "flashcards"
                summary_line.update(
                    f"[bold rgb(255,80,80)]Flashcard Proposal[/]  "
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

        Flips the view's collapsed flag to True, drops every editable descendant out of the focus
        chain (via ``can_focus = False``), and blurs whatever currently holds focus so the chat
        input can take over once the interrupt resolves."""
        self._collapsed = True

        for wid in (
            "fp-shared-topic-setter",
            "fp-details-question",
            "fp-details-answer",
            "fp-details-testing-notes",
            "fp-details-choices",
            "fp-global-choices",
            "fp-edit-instructions",
        ):
            try:
                self.query_one(f"#{wid}", Widget).can_focus = False
            except Exception:
                pass

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

        if target_id == "fp-flashcard-list":
            n = len(self._vm.flashcards)
            if n == 0:
                return
            self._vm.set_cursor(0 if direction == "down" else n - 1)
            return

        if target_id in ("fp-global-choices", "fp-details-choices"):
            widget = self.query_one(f"#{target_id}", ChoiceList)
            n = len(widget.choices())
            widget._cursor = 0 if direction == "down" else max(0, n - 1)
            widget._refresh()

    def _is_node_available(self, node_id: str) -> bool:
        # In DONE only the flashcard-list stays in the graph — the user browses the proposal but
        # can't edit anything.
        if self._vm.state == FlashcardProposalModel.State.DONE:
            return node_id == "fp-flashcard-list"
        if node_id == "fp-details-choices":
            return self._vm.details.is_dirty
        if node_id == "fp-edit-instructions":
            return self._vm.edit_instructions_visible
        return True

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_accept_all(self) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
            return
        self._vm.accept_all()

    def action_reset(self) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
            return
        self._vm.reset()

    def action_cancel(self) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
            return
        self._vm.cancel()

    def action_toggle_edit_instructions(self) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
            return
        self._vm.toggle_edit_instructions_area()
        if self._vm.edit_instructions_visible:
            try:
                self.query_one("#fp-edit-instructions", Widget).focus()
            except Exception:
                pass

    def action_set_topic_all(self) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
            return
        self._open_topic_picker(scope="all")

    def action_toggle_collapsed(self) -> None:
        """DONE-only enter binding. No-ops in EDITING so it can't fight with the EDITING-state
        ``enter`` consumers (SharedTopicSetter, the ConfirmableTextArea accept hook, the
        FlashcardProposalChoices in-list activator)."""
        if self._vm.state != FlashcardProposalModel.State.DONE:
            return
        self._toggle_collapsed()

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "fp-collapse":
            return
        event.stop()
        if self._vm.state != FlashcardProposalModel.State.DONE:
            return
        self._toggle_collapsed()

    def _toggle_collapsed(self) -> None:
        # Flip _collapsed, then swap focus so the enter binding lives on whichever node makes sense
        # for the new state: parent self when collapsed (so enter re-expands), flashcard list when
        # expanded (so the user can browse the proposal).
        self._collapsed = not self._collapsed
        self._refresh_done_surface()
        if self._collapsed:
            self.focus()
        else:
            try:
                self.query_one("#fp-flashcard-list", FlashcardList).focus()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Topic picker
    # ------------------------------------------------------------------

    @on(SetTopicRequested)
    def _on_set_topic_requested(self, event: SetTopicRequested) -> None:
        if self._vm.state != FlashcardProposalModel.State.EDITING:
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
                self._vm.set_current_flashcard_topic(topic_id, topic_name)
            elif scope == "all":
                self._vm.set_topic_all(topic_id, topic_name)

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._session_factory),
            _on_dismiss,
        )
