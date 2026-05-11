from textual.events import Blur, Focus
from textual.widget import Widget
from .view_model_base import ViewModelBase


class ViewBase[T: ViewModelBase](Widget):
    """Minimal MVVM view base. Holds a typed reference to its view-model, wires the standard ``dirty``
    → ``_refresh`` and ``focus`` → ``Widget.focus`` subscriptions, and forwards Textual ``on_focus`` /
    ``on_blur`` events to the VM's ``notify_focused`` / ``notify_blurred`` hooks.

    Subclasses override ``_refresh`` to actually paint based on VM state. Multi-VM views (e.g. a parent
    widget whose VM orchestrates sub-VMs) can subscribe additional handlers in their own ``__init__``
    to those sub-VMs' dirty groups — typically a per-region ``_refresh_<region>`` method, for granular
    repaints.
    """

    def __init__(
        self,
        vm: T,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        markup: bool = True
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
            markup=markup,
        )

        self._vm = vm

        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.subscribe(self._vm.focus, self.focus)

    def on_focus(self, event: Focus) -> None:
        self._vm.notify_focused()

    def on_blur(self, event: Blur) -> None:
        self._vm.notify_blurred()

    def _refresh(self) -> None:
        pass