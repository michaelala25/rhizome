"""Workspace view — the ``PanelOrchestrator`` half of ``WorkspaceModel``.

Lays out the workspace's panel slots and routes each surfaced panel VM to its slot. Two slots exist
today: the resource loader (left) and the chat area (center), laid out side by side; the status-bar
(bottom) and auxiliary (right) slots get created alongside their panel views as they land. Both current
panels are bound at module import (the ``register_panel`` calls below), so the VMs the model surfaces at
bootstrap mount into their slots.

Mounted by ``ChatTabPane`` (one Workspace per chat tab) — it backs the tab's content, with the screen
reaching the conversation through ``pane.workspace`` (e.g. to focus the chat input on a tab switch).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.app.workspace.workspace import WorkspaceModel
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.panel_orchestrator import PanelOrchestrator, PanelSlot, register_panel
from rhizome.tui.widgets.resource_loader import ResourceLoader
from rhizome.utils.services import ServiceAccessor


# Bind the panels (VM type -> view -> slot). Import side effect, populated once.
register_panel(ChatAreaModel, slot="slot-center")(ChatArea)
register_panel(ResourceLoaderModel, slot="slot-left")(ResourceLoader)


class Workspace(PanelOrchestrator[WorkspaceModel]):

    DEFAULT_CSS = """
    Workspace { layout: vertical; height: 1fr; }
    Workspace .ws-body { layout: horizontal; height: 1fr; }
    Workspace #slot-left { width: 56; height: 1fr; }
    Workspace #slot-center { width: 1fr; height: 1fr; }
    """

    def __init__(self, *, services: ServiceAccessor, **kwargs) -> None:
        super().__init__(WorkspaceModel(services), **kwargs)

    def compose(self) -> ComposeResult:
        # Left (resource loader) + center (chat area) side by side. The bottom / right slots are created
        # with their panel views as they land, to avoid an empty docked container reserving dead space.
        left = PanelSlot(id="slot-left")
        center = PanelSlot(id="slot-center")
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

    # TODO(panels): register the remaining panels as their views land, e.g.
    #   register_panel(StatusBarModel, slot="slot-bottom")(StatusBar)

    def hide_focus_target(self, slot_id: str):
        """Where focus lands when ``slot_id`` empties — the chat area's input. TODO: resolve once the
        chat-area panel view exposes its input for focusing."""
        return None
