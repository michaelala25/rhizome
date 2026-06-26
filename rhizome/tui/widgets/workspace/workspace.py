"""Workspace view — the ``PanelOrchestrator`` half of ``WorkspaceModel``.

Lays out the workspace's panel slots and routes each surfaced panel VM to its slot. Two slots exist
today: the chat area (center, always present) and a left slot the resource loader and conversation-graph
viewer share one-at-a-time (opened on demand via ``/resources`` / ``/graph``). The panels are bound at
module import (the ``register_panel`` calls below). slot-left collapses to zero width while empty
(``_sync_slot_visibility``), so a closed left panel reserves no space. (The status bar is not a panel — it
is docked inside the chat area itself.)

Mounted by ``ChatTabPane`` (one Workspace per chat tab) — it backs the tab's content, with the screen
reaching the conversation through ``pane.workspace`` (e.g. to focus the chat input on a tab switch).
"""

from __future__ import annotations

from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.graph_viewer import GraphViewerModel
from rhizome.app.model import ViewModelBase
from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.app.workspace.workspace import WorkspaceModel
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.graph_viewer import GraphViewer
from rhizome.tui.widgets.panel_orchestrator import PanelOrchestrator, PanelSlot, register_panel
from rhizome.tui.widgets.resource_loader import ResourceLoader
from rhizome.tui.widgets.shared.focus_orchestration import Direction, FocusGraph, FocusOrchestrationMixin
from rhizome.utils.services import ServiceAccessor


# Bind the panels (VM type -> view -> slot). Import side effect, populated once.
register_panel(ChatAreaModel, slot="slot-center")(ChatArea)
register_panel(ResourceLoaderModel, slot="slot-left")(ResourceLoader)
register_panel(GraphViewerModel, slot="slot-left")(GraphViewer)


class Workspace(PanelOrchestrator[WorkspaceModel], FocusOrchestrationMixin):
    """Outer-tier focus host over the docked panels: ctrl+left/right hop between slots, composing with the
    chat area's own ctrl+up/down feed graph via Textual's keybinding fall-through (``ChatArea`` binds no
    left/right, so those bubble here). See ``focus_orchestration.py`` for the inner/outer two-tier model."""

    # Mixin listed LAST so ``PanelOrchestrator``'s ``DEFAULT_CSS`` still aggregates (Textual walks only the
    # first DOMNode base at each MRO step); ``can_focus`` set explicitly since ``ViewBase``'s Widget-default
    # would otherwise win MRO over the mixin's default.
    can_focus = True

    # The outer graph: the two docked slots, with the always-present chat area as the resting source.
    # ``slot-left`` is occupancy-gated in ``_is_node_available``, so a static graph suffices — its left edge
    # simply yields no move while the slot is empty.
    FOCUS_GRAPH = FocusGraph(
        source="slot-center",
        edges={
            "slot-center": {"left": "slot-left"},
            "slot-left":   {"right": "slot-center"},
        },
    )

    BINDINGS = [
        Keybind.OuterFocusLeft. as_binding("focus_neighbour('left')",  show=False),
        Keybind.OuterFocusRight.as_binding("focus_neighbour('right')", show=False),
    ]

    DEFAULT_CSS = """
    Workspace { layout: vertical; height: 1fr; }
    Workspace .ws-body { layout: horizontal; height: 1fr; }
    Workspace #slot-left { width: 56; height: 1fr; }
    Workspace #slot-center { width: 1fr; height: 1fr; }
    """

    def __init__(self, *, services: ServiceAccessor, show_welcome: bool = False, **kwargs) -> None:
        super().__init__(WorkspaceModel(services, show_welcome=show_welcome), **kwargs)

    def compose(self) -> ComposeResult:
        # Center (chat area) + left (resource loader) side by side. slot-left starts collapsed: the
        # resource loader opens on demand, and an empty slot reserves no width (see _sync_slot_visibility).
        # The right slot is created with its panel view as it lands, to avoid an empty docked container.
        left = PanelSlot(id="slot-left")
        center = PanelSlot(id="slot-center")
        left.display = False
        self.register_slot("slot-left", left)
        self.register_slot("slot-center", center)
        with Horizontal(classes="ws-body"):
            yield left
            yield center

    def on_mount(self) -> None:
        # Second construction phase: surface the initial panels now that the view (and its app context)
        # exist. No super() — Textual auto-dispatches on_mount across the MRO, so the Orchestrator's own
        # subscribe + replay still runs; its replay mounts whatever bootstrap surfaces here.
        self.model.bootstrap()

    # TODO(panels): register the remaining panels as their views land, e.g. an auxiliary right panel.

    def _on_request_mount(self, vm: ViewModelBase) -> None:
        super()._on_request_mount(vm)
        self._sync_slot_visibility()

    def _on_request_unmount(self, vm: ViewModelBase) -> None:
        super()._on_request_unmount(vm)
        self._sync_slot_visibility()

    def _sync_slot_visibility(self) -> None:
        """Collapse any empty slot so it reserves no space. Driven off ``PanelSlot.current`` after every
        mount/unmount — slot-left (the on-demand resource loader) toggles; slot-center always holds the
        chat area, so it stays shown."""
        for slot in self._slots.values():
            slot.display = slot.current is not None

    # ------------------------------------------------------------------
    # Outer focus navigation
    # ------------------------------------------------------------------
    # Node ids are the slot ids set in ``compose``, so the mixin's ``_current_focus_node`` ancestor-walk maps
    # focus buried inside a panel back to its containing slot for free. The two seams below adapt the slot
    # layer to the graph: a slot is a node only while occupied, and focus must land on the docked *view* (a
    # ``PanelSlot`` is a non-focusable ``Container``) so the view's own ``on_focus`` delegates inward.

    def action_focus_neighbour(self, direction: Direction) -> None:
        if self.focus_neighbour(direction) is None:
            raise SkipAction()

    def _is_node_available(self, node_id: str) -> bool:
        slot = self._slots.get(node_id)
        return slot is not None and slot.current is not None

    def _resolve_node(self, node_id: str) -> Widget | None:
        slot = self._slots.get(node_id)
        return slot.current if slot is not None else None

    def hide_focus_target(self, slot_id: str):
        """When a side slot empties, rest focus on the outer graph's source — the chat area, which then
        delegates inward to its input."""
        return self._resolve_node(self._get_focus_graph().source)
