"""Workspace view — the ``PanelOrchestrator`` half of ``WorkspaceModel``.

Lays out the workspace's panel slots and routes each surfaced panel VM to its slot. Only the center slot
(the chat area) exists today; the resource-viewer (left), status-bar (bottom), and auxiliary (right)
slots get created alongside their panel views — an empty docked slot would otherwise reserve dead space.
The chat-area panel is bound at module import (the ``register_panel`` call below), so the chat area its
model surfaces at bootstrap mounts into the center.

Not yet wired into ``MainScreen`` — that cutover (``ChatTabPane`` mounting a ``Workspace`` instead of a
``ChatPane``) is the next step once this is exercised.
"""

from __future__ import annotations

from textual.app import ComposeResult

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.workspace.workspace import WorkspaceModel
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.panel_orchestrator import PanelOrchestrator, PanelSlot, register_panel
from rhizome.utils.services import ServiceAccessor


# Bind the chat-area panel (VM type -> view -> slot). Import side effect, populated once.
register_panel(ChatAreaModel, slot="slot-center")(ChatArea)


class Workspace(PanelOrchestrator[WorkspaceModel]):

    DEFAULT_CSS = """
    Workspace { layout: vertical; height: 1fr; }
    Workspace #slot-center { width: 1fr; height: 1fr; }
    """

    def __init__(self, *, services: ServiceAccessor, **kwargs) -> None:
        super().__init__(WorkspaceModel(services), **kwargs)

    def compose(self) -> ComposeResult:
        # Only the center slot exists today. Side / bottom slots are created with their panel views to
        # avoid an empty docked container reserving dead space (see module docstring).
        center = PanelSlot(id="slot-center")
        self.register_slot("slot-center", center)
        yield center

    def on_mount(self) -> None:
        # Second construction phase: surface the initial panels now that the view (and its app context)
        # exist. No super() — Textual auto-dispatches on_mount across the MRO, so the Orchestrator's own
        # subscribe + replay still runs; its replay mounts whatever bootstrap surfaces here.
        self.model.bootstrap()

    # TODO(panels): register the remaining panels as their views land, e.g.
    #   register_panel(ResourceViewerModel, slot="slot-left")(ResourceViewer)
    #   register_panel(StatusBarModel,      slot="slot-bottom")(StatusBar)

    def hide_focus_target(self, slot_id: str):
        """Where focus lands when ``slot_id`` empties — the chat area's input. TODO: resolve once the
        chat-area panel view exposes its input for focusing."""
        return None
