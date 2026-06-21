from __future__ import annotations

from typing import Callable

from rhizome.app.model import ViewModelBase


class OrchestratorModel(ViewModelBase):
    """A view-model that owns a set of child view-models and decides which of them are surfaced.

    Custody
    -------
    An orchestrator holds its children with *lazy, type-keyed custody*: a child is built on first
    request (``_get_view_model``), there is exactly one instance per type, and that instance is kept
    alive for as long as the orchestrator lives. A child's load / cursor / scroll state therefore
    survives both the orchestrator *view* being torn down and rebuilt, and the child being hidden —
    custody is independent of visibility. Subclasses register a factory per child type in ``__init__``
    and expose typed accessors layered over ``_get_view_model``.

    Surfacing
    ---------
    The orchestrator keeps an ordered list of which children are currently surfaced — the authoritative
    "what is open" record — and mutates it through ``request_mount`` / ``request_unmount`` / ``toggle``.
    Each mutation emits a directive (``RequestMount`` / ``RequestUnmount``, carrying the child VM) that
    the orchestrator view fulfils by mounting or unmounting that child's view.

    Layout-agnostic
    ---------------
    The model knows *what* is surfaced and in what order, never *where* a child is placed: the surfaced
    record carries no region / slot / coordinate information, because placement is a view concern. The
    order is "most recently surfaced last" — what a rebuilt view replays to restore the open children,
    and what gives a well-defined winner should a placement policy map two surfaced children to one
    location.
    """

    class Callbacks(ViewModelBase.Callbacks):
        RequestMount   = "RequestMount"      # payload: the child VM to surface (mount its view)
        RequestUnmount = "RequestUnmount"    # payload: the child VM to hide (unmount its view)

    def __init__(self) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.RequestMount:   ViewModelBase,
            self.Callbacks.RequestUnmount: ViewModelBase,
        })

        # type -> factory(self) -> VM. A bare-type registration uses the class as its own zero-arg
        # factory; descriptors that need session / manager / graph deps close over the model passed in.
        self._vm_descriptors: dict[type[ViewModelBase], Callable[[OrchestratorModel], ViewModelBase]] = {}

        # type -> the one live instance. Strong refs, so a child hidden from the screen keeps its
        # load / cursor / scroll state for the session — custody is independent of visibility.
        self._vms: dict[type[ViewModelBase], ViewModelBase] = {}

        # Surfaced child instances in surfacing order (most recent last). The authoritative "what is
        # open" record; region-free, since placement is the view's concern. A rebuilt view replays it.
        self._surfaced: list[ViewModelBase] = []

    # -- custody -----------------------------------------------------------------------------------

    def _register_view_model[T: ViewModelBase](
        self,
        vm_cls: type[T],
        descriptor: Callable[[OrchestratorModel], T] | None = None,
    ) -> None:
        """Register how to build the single instance of ``vm_cls``. Omit ``descriptor`` when the VM
        takes no constructor args — the class becomes its own zero-arg factory."""
        if vm_cls in self._vm_descriptors:
            raise ValueError(f"A descriptor is already registered for {vm_cls.__name__}.")
        self._vm_descriptors[vm_cls] = descriptor or (lambda _self: vm_cls())

    def _get_view_model[T: ViewModelBase](self, vm_cls: type[T]) -> T:
        """Get-or-build the single instance of ``vm_cls`` and cache it for the orchestrator's lifetime."""
        inst = self._vms.get(vm_cls)
        if inst is None:
            descriptor = self._vm_descriptors.get(vm_cls)
            if descriptor is None:
                raise LookupError(f"No descriptor registered for {vm_cls.__name__}.")
            inst = descriptor(self)
            self._vms[vm_cls] = inst
        return inst  # type: ignore[return-value]

    # -- surfacing ---------------------------------------------------------------------------------

    def request_mount(self, vm: ViewModelBase) -> None:
        """Surface ``vm`` — move it to the front of the surfacing order and emit ``RequestMount``.

        Emits even when ``vm`` is already frontmost: the view's handler is idempotent and, finding the
        child already shown, simply refocuses it. That is how "re-invoke to refocus the open panel"
        works without special-casing."""
        if vm in self._surfaced:
            self._surfaced.remove(vm)
        self._surfaced.append(vm)
        self.emit(self.Callbacks.RequestMount, vm)

    def request_unmount(self, vm: ViewModelBase) -> None:
        """Hide ``vm`` and emit ``RequestUnmount``. No-op (and no emit) if it was not surfaced."""
        if vm not in self._surfaced:
            return
        self._surfaced.remove(vm)
        self.emit(self.Callbacks.RequestUnmount, vm)

    def toggle(self, vm_cls: type[ViewModelBase]) -> None:
        """Hide the surfaced panel for ``vm_cls`` if there is one, otherwise build it and surface it.

        Checks the surfaced list *before* resolving the instance, so toggling a never-built panel
        closed doesn't construct it just to discover there is nothing to hide."""
        for vm in self._surfaced:
            if type(vm) is vm_cls:
                self.request_unmount(vm)
                return
        self.request_mount(self._get_view_model(vm_cls))

    def surfaced_view_models(self) -> tuple[ViewModelBase, ...]:
        """The surfaced child instances in surfacing order (most recent last)."""
        return tuple(self._surfaced)
