"""Workspace view — the ``PanelOrchestrator`` half of ``WorkspaceModel``.

Lays out the workspace's panel slots and routes each surfaced panel VM to its slot. Two slots exist
today: the chat area (center, always present) and the resource loader (left, opened on demand via
``/resources``); an auxiliary (right) slot gets created alongside its panel view as it lands. Both panels
are bound at module import (the ``register_panel`` calls below). slot-left collapses to zero width while
empty (``_sync_slot_visibility``), so the closed resource loader reserves no space. (The status bar is not
a panel — it is docked inside the chat area itself.)

Mounted by ``ChatTabPane`` (one Workspace per chat tab) — it backs the tab's content, with the screen
reaching the conversation through ``pane.workspace`` (e.g. to focus the chat input on a tab switch).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches

from rhizome.app.chat_area.chat_area import ChatAreaModel
from rhizome.app.model import ViewModelBase
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

    def hide_focus_target(self, slot_id: str):
        """When a side slot empties, hand focus back to the chat area's input."""
        center = self._slots.get("slot-center")
        if center is None or center.current is None:
            return None
        try:
            return center.current.query_one("#chat-input")
        except NoMatches:
            return None
