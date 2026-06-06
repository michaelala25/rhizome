from textual.events import Blur, Focus
from textual.widget import Widget
from rhizome.app.model import ViewModelBase


class ViewBase[T: ViewModelBase](Widget):
    """Minimal MVVM view base. Holds a typed reference to its view-model, wires the standard ``dirty``
    → ``_refresh`` and ``focus`` → ``Widget.focus`` subscriptions, and forwards Textual ``on_focus`` /
    ``on_blur`` events to the VM's ``notify_focused`` / ``notify_blurred`` hooks.

    Subclasses override ``_refresh`` to actually paint based on VM state. Multi-VM views (e.g. a parent
    widget whose VM orchestrates sub-VMs) can subscribe additional handlers in their own ``__init__``
    to those sub-VMs' dirty groups — typically a per-region ``_refresh_<region>`` method, for granular
    repaints.

    Subscriptions are wired at construction and torn down on unmount. This is safe for the common case
    where the VM and the view share a lifetime, and also handles the long-lived-VM case (a VM that
    outlives several view instances across destroy/recreate cycles) — without ``on_unmount``, every
    destroyed view's callback would stay subscribed to the VM forever.
    """

    def __init__(self, vm: T, *children: Widget, **kwargs) -> None:
        super().__init__(*children, **kwargs)

        self._vm = vm

        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.subscribe(self._vm.focus, self.focus)

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.unsubscribe(self._vm.focus, self.focus)

    def on_focus(self, event: Focus) -> None:
        self._vm.notify_focused()

    def on_blur(self, event: Blur) -> None:
        self._vm.notify_blurred()

    def _refresh(self) -> None:
        pass
