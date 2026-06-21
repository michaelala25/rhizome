from __future__ import annotations

from rhizome.app.model import ViewModelBase
from rhizome.app.orchestrator import OrchestratorModel
from rhizome.tui.widgets.view_base import ViewBase


class Orchestrator[U: OrchestratorModel](ViewBase[U]):
    """The view half of an ``OrchestratorModel``: realizes its surfacing directives as mounted child views.

    On mount it subscribes to the model's ``RequestMount`` / ``RequestUnmount`` directives and then
    replays whatever the model already holds surfaced, so a freshly built orchestrator view restores the
    open children — the model outlives any one view and carries the surfaced record across the rebuild.
    The subscriptions are dropped on unmount; this is the ordinary ``ViewBase`` lifetime, one view bound
    to its own model.

    This base knows nothing about *where* a child's view goes. Subclasses implement ``_on_request_mount``
    / ``_on_request_unmount`` to place and remove views under whatever layout policy they impose; the
    same handler serves both a live directive and the on-mount replay.
    """

    def on_mount(self) -> None:
        # Textual auto-dispatches on_mount across the MRO, so ``ViewBase``'s own wiring still runs and
        # this needs no super() call. Subclasses build their containers in ``compose`` (always before
        # on_mount), so the replay below lands in live containers — that replay is how a rebuilt
        # orchestrator view restores the children the model still considers surfaced.
        m = self.model
        m.subscribe(m.Callbacks.RequestMount, self._on_request_mount)
        m.subscribe(m.Callbacks.RequestUnmount, self._on_request_unmount)
        for vm in m.surfaced_view_models():
            self._on_request_mount(vm)

    def on_unmount(self) -> None:
        m = self.model
        m.unsubscribe(m.Callbacks.RequestMount, self._on_request_mount)
        m.unsubscribe(m.Callbacks.RequestUnmount, self._on_request_unmount)

    def _on_request_mount(self, vm: ViewModelBase) -> None:
        raise NotImplementedError("Orchestrator subclasses decide where a surfaced view-model is placed.")

    def _on_request_unmount(self, vm: ViewModelBase) -> None:
        raise NotImplementedError("Orchestrator subclasses decide how a hidden view-model is removed.")
