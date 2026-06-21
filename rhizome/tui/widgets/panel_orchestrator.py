"""Slot-based panel orchestration: a ``PanelOrchestrator`` docks each surfaced panel into a ``PanelSlot``.

A *panel* is a view-model type bound — via ``register_panel`` — to the view that renders it and the
slot it docks into. A *slot* (``PanelSlot``) is a docked container that holds one panel at a time. A
``PanelOrchestrator`` routes each view-model its model surfaces to that view-model's slot, mounting,
swapping, and focusing panel views in response to the model's ``RequestMount`` / ``RequestUnmount``
directives.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.containers import Container

from rhizome.app.model import ViewModelBase
from rhizome.app.orchestrator import OrchestratorModel
from rhizome.tui.widgets.orchestrator import Orchestrator
from rhizome.tui.widgets.view_base import ViewBase


# ========================================================================================================
# PANEL REGISTRY
# ========================================================================================================

@dataclass(frozen=True)
class PanelRegistration:
    """Binds a view-model type to the view that renders it and the slot it docks into.

    ``slot`` is registration metadata, kept here rather than on the view class so the same view stays
    usable outside a ``PanelOrchestrator`` (it carries no dangling slot id) while a panel still declares
    its own placement at the point of registration.
    """
    vm_cls: type[ViewModelBase]
    view_cls: type[ViewBase]
    slot: str


# Global, import-populated. One panel per view-model type.
_PANEL_REGISTRY: dict[type[ViewModelBase], PanelRegistration] = {}


def register_panel(vm_cls: type[ViewModelBase], *, slot: str):
    """Bind ``view_cls`` as the panel for ``vm_cls``, docking into ``slot``. Populated as an import
    side effect: a panel module declares its registration at class-definition time. First-party panels
    can register imperatively as ``register_panel(VM, slot=...)(View)``."""
    def deco(view_cls: type[ViewBase]) -> type[ViewBase]:
        if vm_cls in _PANEL_REGISTRY:
            raise ValueError(f"A panel is already registered for {vm_cls.__name__}.")
        _PANEL_REGISTRY[vm_cls] = PanelRegistration(vm_cls, view_cls, slot)
        return view_cls
    return deco


def panel_for(vm: ViewModelBase) -> PanelRegistration | None:
    """The panel registered for ``vm``'s exact type, or ``None`` — exact-type lookup, no base fallback."""
    return _PANEL_REGISTRY.get(type(vm))


# ========================================================================================================
# PANEL SLOT + PANEL ORCHESTRATOR
# ========================================================================================================

class PanelSlot(Container):
    """A docked container hosting at most one panel view. ``current`` tracks the mounted view so the
    orchestrator can compare / swap / focus it without a query (and synchronously, before Textual has
    processed the mount)."""
    current: ViewBase | None = None


class PanelOrchestrator[U: OrchestratorModel](Orchestrator[U]):
    """An orchestrator that docks each surfaced panel into a named ``PanelSlot``, one panel per slot.

    Slots are registered from ``compose`` (``register_slot``); each surfaced view-model is routed to the
    slot named by its ``PanelRegistration``. Re-surfacing the panel already in a slot refocuses it;
    surfacing a different panel into an occupied slot evicts the current one.

    One view-model type per slot is the supported configuration. If two types were registered to the
    same slot, surfacing one would remove the other from the screen while it remained in the model's
    surfaced set; resolving that (evict-and-forget vs. a reveal-on-hide stack) is a placement decision
    deferred until a slot actually hosts more than one type.
    """

    def __init__(self, vm: U, *children, **kwargs) -> None:
        super().__init__(vm, *children, **kwargs)
        self._slots: dict[str, PanelSlot] = {}

    def register_slot(self, slot_id: str, slot: PanelSlot) -> None:
        if slot_id in self._slots:
            raise ValueError(f"Slot {slot_id!r} is already registered.")
        self._slots[slot_id] = slot

    def _on_request_mount(self, vm: ViewModelBase) -> None:
        reg = panel_for(vm)
        if reg is None:
            raise LookupError(f"No panel registered for {type(vm).__name__}.")
        slot = self._slots.get(reg.slot)
        if slot is None:
            raise LookupError(f"Unknown slot {reg.slot!r} requested by {type(vm).__name__}.")

        # Already showing this exact panel → refocus it (the re-invoke path). Checked before building,
        # so no throwaway view is constructed and the panel's scroll / cursor state survives.
        if slot.current is not None and slot.current.model is vm:
            slot.current.focus()
            return

        if slot.current is not None:
            slot.current.remove()
            slot.current = None

        view = reg.view_cls(vm)
        slot.mount(view)
        slot.current = view
        # Focus after compose — the panel's inner widgets aren't mounted until the next refresh.
        view.call_after_refresh(view.focus)

    def _on_request_unmount(self, vm: ViewModelBase) -> None:
        for slot_id, slot in self._slots.items():
            if slot.current is not None and slot.current.model is vm:
                slot.current.remove()
                slot.current = None
                target = self.hide_focus_target(slot_id)
                if target is not None:
                    target.focus()
                return

    def hide_focus_target(self, slot_id: str):
        """Hook: the widget to focus when ``slot_id`` empties (return ``None`` to leave focus alone)."""
        return None
