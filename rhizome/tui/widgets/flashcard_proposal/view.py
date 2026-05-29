"""``FlashcardProposal`` — parent view for ``FlashcardProposalVM``.

Composes the five focusable regions and orchestrates inter-region focus via a static graph driven
by ``alt+arrow`` bindings. Leaves whose plain-arrow bindings are inactive at a boundary (via
``check_action``) — or that simply don't bind the arrow keys — let the keystroke flow up to this
view's own arrow handlers, which forward focus along the graph. The parent also owns the
topic-picker modal (since the modal needs a ``session_factory`` and the leaves do not) and the
lifecycle keybindings (``ctrl+a/r/c``, ``ctrl+e``, ``shift+t``).

The focus graph
---------------
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
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

from rhizome.app.flashcard_proposal.flashcard_proposal import FlashcardProposalVM
from rhizome.tui.widgets.flashcard_proposal.choices import FlashcardProposalChoices
from rhizome.tui.widgets.flashcard_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.flashcard_proposal.flashcard_details import FlashcardDetails
from rhizome.tui.widgets.flashcard_proposal.flashcard_list import FlashcardList
from rhizome.tui.widgets.flashcard_proposal.messages import SetTopicRequested
from rhizome.tui.widgets.flashcard_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.view_base import ViewBase


# Static focus graph. Each entry maps a node id to its alt+arrow neighbours. ``None`` means "no
# neighbour in this direction." Some neighbours are conditional on VM state (details-choices
# visible only while the per-flashcard edit is dirty; edit-instructions visible only when the
# area is open); ``_resolve_neighbour`` filters those out at lookup time.
_FOCUS_GRAPH: dict[str, dict[str, str | None]] = {
    "fp-shared-topic-setter": {
        "up": None,
        "down": "fp-flashcard-list",
        "left": None,
        "right": None,
    },
    "fp-flashcard-list": {
        "up": "fp-shared-topic-setter",
        "down": "fp-global-choices",
        "left": None,
        "right": "fp-details-question",
    },
    "fp-details-question": {
        "up": "fp-shared-topic-setter",
        "down": "fp-details-answer",
        "left": "fp-flashcard-list",
        "right": None,
    },
    "fp-details-answer": {
        "up": "fp-details-question",
        "down": "fp-details-testing-notes",
        "left": "fp-flashcard-list",
        "right": None,
    },
    "fp-details-testing-notes": {
        "up": "fp-details-answer",
        "down": "fp-details-choices",  # falls through to global-choices if not visible
        "left": "fp-flashcard-list",
        "right": None,
    },
    "fp-details-choices": {
        "up": "fp-details-testing-notes",
        "down": "fp-global-choices",
        "left": "fp-flashcard-list",
        "right": None,
    },
    "fp-global-choices": {
        "up": "fp-flashcard-list",
        "down": "fp-edit-instructions",  # falls through to None if not visible
        "left": None,
        "right": None,
    },
    "fp-edit-instructions": {
        "up": "fp-global-choices",
        "down": None,
        "left": None,
        "right": None,
    },
}


class FlashcardProposal(ViewBase[FlashcardProposalVM]):
    """Parent view for the flashcard-proposal interrupt."""

    DEFAULT_CSS = """
    FlashcardProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        max-height: 40;
        padding: 0;
    }
    FlashcardProposal #fp-title {
        height: 1;
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
    FlashcardProposal #fp-middle {
        height: auto;
        padding: 0 1;
    }
    FlashcardProposal #fp-global-choices {
        margin: 1 0;
    }
    """

    BINDINGS = [
        # alt+arrows: always-on focus orchestration (works from any focused descendant).
        Binding("alt+up", "focus_neighbour('up')", show=False),
        Binding("alt+down", "focus_neighbour('down')", show=False),
        Binding("alt+left", "focus_neighbour('left')", show=False),
        Binding("alt+right", "focus_neighbour('right')", show=False),
        # Plain up/down catch-all: leaves either don't bind these (SharedTopicSetter) or report
        # them as inactive via ``check_action`` when at a boundary (FlashcardList,
        # FlashcardProposalChoices). Either way the key bubbles here.
        Binding("up", "navigate_cursor('up')", show=False),
        Binding("down", "navigate_cursor('down')", show=False),
        # ``e`` from anywhere that doesn't consume it (i.e. FlashcardList) jumps focus into the
        # details panel, equivalent to alt+right from the flashcard list.
        Binding("e", "focus_neighbour('right')", show=False),
        Binding("ctrl+a", "accept_all", show=False),
        Binding("ctrl+r", "reset", show=False),
        Binding("ctrl+c", "cancel", show=False),
        Binding("ctrl+e", "toggle_edit_instructions", show=False),
        Binding("shift+t", "set_topic_all", show=False),
    ]

    def __init__(self, vm: FlashcardProposalVM, *, session_factory: Any | None = None, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        # Used by the topic-picker modal. ``None`` disables the modal — useful for unit tests and
        # for the ``/test-flashcard-proposal`` slash command which doesn't need real topic data.
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        yield Static(self._title_text(), id="fp-title")
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
        yield EditInstructionsArea(self._vm, id="fp-edit-instructions")

    def _flashcard_list_hints_text(self) -> str:
        rows = [
            ("t", "set topic"),
            ("shift+t", "set topic for all"),
            ("d", "exclude"),
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
        # no-op. We override on_mount to land focus on the FlashcardList per the spec.
        self._focus_first()
        # Drives the programmatic flashcard-area↔details height pin. ``vm.details.dirty`` fires
        # when the focused flashcard changes or its content edits land — both can shift the
        # details panel's rendered height, which is what we're tracking.
        self._vm.details.subscribe(self._vm.details.dirty, self._sync_list_area_height)

    def on_unmount(self) -> None:
        self._vm.details.unsubscribe(self._vm.details.dirty, self._sync_list_area_height)

    def on_resize(self) -> None:
        self._sync_list_area_height()

    def _sync_list_area_height(self) -> None:
        # Pin flashcard-list-area's height to ``max(its own natural content height, details
        # height)`` so a tall FlashcardDetails doesn't leave dead vertical space between the
        # bordered table and the FlashcardProposalChoices row below. Runs after layout settles so
        # children sizes are real.
        def _apply() -> None:
            try:
                area = self.query_one("#fp-flashcard-list-area", Vertical)
                details = self.query_one("#fp-flashcard-details", Widget)
                flashcard_list = self.query_one("#fp-flashcard-list", FlashcardList)
            except Exception:
                return
            # FlashcardList + hints (1 row) + border (top + bottom = 2). Take FlashcardList's
            # actual rendered height so this tracks the auto-sized table content.
            natural = flashcard_list.size.height + 1 + 2
            target = max(natural, details.size.height)
            area.styles.height = target
        self.call_after_refresh(_apply)

    def _refresh(self) -> None:
        # Parent has no own-rendered content. Children subscribe to vm.dirty independently.
        pass

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def _focus_first(self) -> None:
        self.query_one("#fp-flashcard-list", FlashcardList).focus()

    def action_focus_neighbour(self, direction: str) -> None:
        """Hard refocus along the focus graph (``alt+arrow``). Leaves target cursors untouched."""
        focused = self.screen.focused if self.screen is not None else None
        source_id = self._owning_focus_node_id(focused)
        target_id = self._resolve_neighbour(source_id, direction)
        if target_id is None:
            return
        self._focus_node(target_id)

    def action_navigate_cursor(self, direction: str) -> None:
        """Plain ``up``/``down`` cross-region jump — refocuses and resets the target's cursor to
        the natural entry side (top when arriving from above, bottom from below)."""
        focused = self.screen.focused if self.screen is not None else None
        source_id = self._owning_focus_node_id(focused)
        target_id = self._resolve_neighbour(source_id, direction)
        if target_id is None:
            return
        self._focus_node(target_id)
        self._reset_target_cursor_for_continuation(target_id, direction)

    def _reset_target_cursor_for_continuation(self, target_id: str, direction: str) -> None:
        from rhizome.tui.widgets.browser.shared.choices_list import ChoiceList

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

    def _owning_focus_node_id(self, widget: Widget | None) -> str | None:
        """Walk up from ``widget`` until we hit a node that's in the focus graph. Handles the case
        where the actually-focused widget is a nested TextArea inside ``FlashcardDetails`` — we
        want to identify which graph node it lives under."""
        node: Widget | None = widget
        while node is not None and node is not self:
            wid = node.id
            if wid in _FOCUS_GRAPH:
                return wid
            node = node.parent
        return None

    def _resolve_neighbour(self, source_id: str | None, direction: str) -> str | None:
        if source_id is None or source_id not in _FOCUS_GRAPH:
            return None
        target = _FOCUS_GRAPH[source_id].get(direction)
        # State-conditional skip: hop past nodes that are currently hidden.
        while target is not None and not self._is_focus_node_available(target):
            next_hop = _FOCUS_GRAPH.get(target, {}).get(direction)
            target = next_hop
        return target

    def _is_focus_node_available(self, node_id: str) -> bool:
        if node_id == "fp-details-choices":
            return self._vm.details.is_dirty
        if node_id == "fp-edit-instructions":
            return self._vm.edit_instructions_visible
        return True

    def _focus_node(self, node_id: str) -> None:
        # ``FlashcardDetails`` is a container — its graph nodes map to three TextAreas + a
        # ChoiceList. Map by the actual graph-node id, which is the id we put on the individual
        # children.
        try:
            widget = self.query_one(f"#{node_id}", Widget)
        except Exception:
            return
        widget.focus()

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_accept_all(self) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self._vm.accept_all()

    def action_reset(self) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self._vm.reset()

    def action_cancel(self) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self._vm.cancel()

    def action_toggle_edit_instructions(self) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self._vm.toggle_edit_instructions_area()
        if self._vm.edit_instructions_visible:
            self._focus_node("fp-edit-instructions")

    def action_set_topic_all(self) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
            return
        self._open_topic_picker(scope="all")

    # ------------------------------------------------------------------
    # Topic picker
    # ------------------------------------------------------------------

    def on_set_topic_requested(self, event: SetTopicRequested) -> None:
        if self._vm.state != FlashcardProposalVM.State.EDITING:
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
