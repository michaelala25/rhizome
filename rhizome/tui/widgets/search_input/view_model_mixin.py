"""SearchableViewModelMixin — declares the narrow VM contract a ``SearchInput`` needs.

The widget treats its VM purely as a black box that accepts ``set_search(query)``; this mixin
captures that requirement at the type level so any VM that wants to drive a ``SearchInput`` can
opt in by mixing it into its concrete class. The mixin is a ``ViewModelBase`` subclass so the
type bound on the generic widget is satisfied without imposing a parallel base hierarchy on
concrete VMs.

Inheritance convention
----------------------
Only the leaf VM that actually drives a ``SearchInput`` should mix this in — never an
intermediate base. That keeps unrelated tabs / panels (whose backing query has no search axis)
out of the abstract obligation, and keeps the MRO local to the one class that needs it. The
mixin adds no state of its own and inherits ``ViewModelBase.__init__`` unchanged, so adding it
to an MRO with another ``ViewModelBase`` ancestor (via cooperative ``super().__init__()``) is
safe.
"""

from __future__ import annotations

from abc import abstractmethod

from ..view_model_base import ViewModelBase


class SearchableViewModelMixin(ViewModelBase):
    """Marker mixin for VMs that own a search query the user can edit via a ``SearchInput``.

    Concrete VMs must implement ``set_search(query)`` so the widget can push the user's
    submitted query into VM state. Whatever the VM does in response (debounced refetch, in-
    memory filter, etc.) is its own business — the widget only knows about the entry point.
    """

    @abstractmethod
    def set_search(self, query: str) -> None:
        """Push a new search query into VM state. Empty string clears the filter."""
