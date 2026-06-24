"""Registry mapping feed-entry view-model types to their view widgets.

The chat area renders an open, runtime-varying set of view-models through one feed. Rather than the
chat area statically knowing every concrete type, each view declares which VM it renders by decorating
itself with ``@register_feed_view(SomeVM)``; the chat area then dispatches with ``view_for(vm)``.

THE REGISTRY IS POPULATED AS A SIDE EFFECT OF IMPORTING THE VIEW MODULES.
A decorator only runs when its module is imported, so nothing is registered until something imports
the views. That "something" is ``feed_views.py`` — import it (the chat area does) before calling
``view_for``, or every lookup returns ``None``.

Lookup is by exact runtime type (no base-class fallback): every feed VM maps 1:1 to a concrete view.
"""

from __future__ import annotations

from rhizome.app.model import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


_view_for_vm: dict[type, type[ViewBase]] = {}


def register_feed_view(*vm_types: type):
    """Decorator registering ``view_cls`` as the view for each of ``vm_types``.

    Apply on a view class for chat-area-native widgets, or call imperatively —
    ``register_feed_view(BrowserModel)(Browser)`` — to register a foreign widget the chat area adopts
    without coupling that widget to this module.
    """
    def deco(view_cls: type[ViewBase]) -> type[ViewBase]:
        for vm_type in vm_types:
            _view_for_vm[vm_type] = view_cls
        return view_cls
    return deco


def view_for(vm: ViewModelBase) -> type[ViewBase] | None:
    """Return the view class registered for ``vm``'s exact type, or ``None`` if unregistered."""
    return _view_for_vm.get(type(vm))
