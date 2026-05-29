"""``CommitProposal`` — parent view for ``CommitProposalVM``.

Composes the five focusable regions and orchestrates inter-region focus via a static graph driven
by ``alt+arrow`` bindings. Leaf widgets post ``BoundaryHit`` when plain arrow navigation hits an
edge; the parent looks up the source in the focus graph and forwards focus to the right neighbour.
The parent also owns the topic-picker modal (since the modal needs a ``session_factory`` and the
leaves do not) and the lifecycle keybindings (``ctrl+a/r/c``, ``ctrl+e``, ``shift+t``).

The focus graph
---------------
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
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

from rhizome.app.commit_proposal.commit_proposal import CommitProposalVM
from rhizome.tui.widgets.commit_proposal.choices import CommitProposalChoices
from rhizome.tui.widgets.commit_proposal.edit_instructions import EditInstructionsArea
from rhizome.tui.widgets.commit_proposal.entry_details import EntryDetails
from rhizome.tui.widgets.commit_proposal.entry_list import EntryList
from rhizome.tui.widgets.commit_proposal.messages import SetTopicRequested
from rhizome.tui.widgets.commit_proposal.shared_topic_setter import SharedTopicSetter
from rhizome.tui.widgets.navigable_feed_item_view_base import NavigableFeedItemViewBase


# Static focus graph. Each entry maps a node id to its alt+arrow neighbours. ``None`` means "no
# neighbour in this direction." Some neighbours are conditional on VM state (entry-details-choices
# visible only while the per-entry edit is dirty; edit-instructions visible only when the area is
# open); ``_resolve_neighbour`` filters those out at lookup time.
_FOCUS_GRAPH: dict[str, dict[str, str | None]] = {
    "cp-shared-topic-setter": {
        "up": None,
        "down": "cp-entry-list",
        "left": None,
        "right": None,
    },
    "cp-entry-list": {
        "up": "cp-shared-topic-setter",
        "down": "cp-global-choices",
        "left": None,
        "right": "cp-details-title",
    },
    "cp-details-title": {
        "up": "cp-shared-topic-setter",
        "down": "cp-details-content",
        "left": "cp-entry-list",
        "right": None,
    },
    "cp-details-content": {
        "up": "cp-details-title",
        "down": "cp-details-choices",  # falls through to global-choices if not visible
        "left": "cp-entry-list",
        "right": None,
    },
    "cp-details-choices": {
        "up": "cp-details-content",
        "down": "cp-global-choices",
        "left": "cp-entry-list",
        "right": None,
    },
    "cp-global-choices": {
        "up": "cp-entry-list",
        "down": "cp-edit-instructions",  # falls through to None if not visible
        "left": None,
        "right": None,
    },
    "cp-edit-instructions": {
        "up": "cp-global-choices",
        "down": None,
        "left": None,
        "right": None,
    },
}


class CommitProposal(NavigableFeedItemViewBase[CommitProposalVM]):
    """Parent view for the commit-proposal interrupt."""

    DEFAULT_CSS = """
    CommitProposal {
        layout: vertical;
        background: transparent;
        height: auto;
        max-height: 35;
        padding: 0;
    }
    CommitProposal #cp-title {
        height: 1;
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
    CommitProposal #cp-middle {
        height: auto;
        padding: 0 1;
    }
    CommitProposal #cp-global-choices {
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
        # them as inactive via ``check_action`` when at a boundary (EntryList) / always inactive
        # (CommitProposalChoices). Either way the key bubbles here.
        Binding("up", "navigate_cursor('up')", show=False),
        Binding("down", "navigate_cursor('down')", show=False),
        # ``e`` from anywhere that doesn't consume it (i.e. EntryList) jumps focus into the
        # details panel, equivalent to alt+right from the entry list.
        Binding("e", "focus_neighbour('right')", show=False),
        Binding("ctrl+a", "accept_all", show=False),
        Binding("ctrl+r", "reset", show=False),
        Binding("ctrl+c", "cancel", show=False),
        Binding("ctrl+e", "toggle_edit_instructions", show=False),
        Binding("shift+t", "set_topic_all", show=False),
    ]

    def __init__(self, vm: CommitProposalVM, *, session_factory: Any | None = None, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        # Used by the topic-picker modal. ``None`` disables the modal — useful for unit tests and
        # for the ``/test-commit-proposal`` slash command which doesn't need real topic data.
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        yield Static(self._title_text(), id="cp-title")
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
        yield EditInstructionsArea(self._vm, id="cp-edit-instructions")

    def _entry_list_hints_text(self) -> str:
        # Key/label colour pair shared with the browser entries tab's keybindings row.
        rows = [
            ("f", "change type"),
            ("t", "set topic"),
            ("shift+t", "set topic for all"),
            ("d", "exclude"),
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
        # We override to land focus on the EntryList per the spec.
        self._focus_first()
        # Drives the programmatic entry-area↔details height pin. ``vm.details.dirty`` fires when
        # the focused entry changes or its content edits land — both can shift the details panel's
        # rendered height, which is what we're tracking.
        self._vm.details.subscribe(self._vm.details.dirty, self._sync_entry_area_height)

    def on_unmount(self) -> None:
        self._vm.details.unsubscribe(self._vm.details.dirty, self._sync_entry_area_height)

    def on_resize(self) -> None:
        self._sync_entry_area_height()

    def _sync_entry_area_height(self) -> None:
        # Pin entry-list-area's height to ``max(its own natural content height, details height)``
        # so a tall EntryDetails doesn't leave dead vertical space between the bordered table and
        # the CommitProposalChoices row below. Runs after layout settles so children sizes are real.
        def _apply() -> None:
            try:
                area = self.query_one("#cp-entry-list-area", Vertical)
                details = self.query_one("#cp-entry-details", Widget)
                entry_list = self.query_one("#cp-entry-list", EntryList)
            except Exception:
                return
            # EntryList + hints (1 row) + border (top + bottom = 2). Take EntryList's actual
            # rendered height so this tracks the auto-sized table content.
            natural = entry_list.size.height + 1 + 2
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
        self.query_one("#cp-entry-list", EntryList).focus()

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

    def _owning_focus_node_id(self, widget: Widget | None) -> str | None:
        """Walk up from ``widget`` until we hit a node that's in the focus graph. Handles the case
        where the actually-focused widget is a nested TextArea inside ``EntryDetails`` — we want to
        identify which graph node it lives under."""
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
            # Continue traversing in the same direction from the hidden node so we land on the
            # first available downstream / upstream neighbour.
            next_hop = _FOCUS_GRAPH.get(target, {}).get(direction)
            target = next_hop
        return target

    def _is_focus_node_available(self, node_id: str) -> bool:
        if node_id == "cp-details-choices":
            return self._vm.details.is_dirty
        if node_id == "cp-edit-instructions":
            return self._vm.edit_instructions_visible
        return True

    def _focus_node(self, node_id: str) -> None:
        # ``EntryDetails`` is a container — its graph-id maps to two TextAreas + a ChoiceList. Map
        # by the actual graph-node id, which is the id we put on the individual children.
        try:
            widget = self.query_one(f"#{node_id}", Widget)
        except Exception:
            return
        widget.focus()

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_accept_all(self) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._vm.accept_all()

    def action_reset(self) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._vm.reset()

    def action_cancel(self) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._vm.cancel()

    def action_toggle_edit_instructions(self) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._vm.toggle_edit_instructions_area()
        if self._vm.edit_instructions_visible:
            self._focus_node("cp-edit-instructions")

    def action_set_topic_all(self) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
            return
        self._open_topic_picker(scope="all")

    # ------------------------------------------------------------------
    # Topic picker
    # ------------------------------------------------------------------

    def on_set_topic_requested(self, event: SetTopicRequested) -> None:
        if self._vm.state != CommitProposalVM.State.EDITING:
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
