"""SortableViewModelMixin — narrow VM contract a ``SortDialog`` needs.

The widget treats its VM as a black box exposing four members: ``sort_options()`` for the
axes it should surface (in display order), ``sort_by`` / ``sort_dir`` for the currently active
sort, and ``set_sort(sort_by, sort_dir)`` to apply a new one. Concrete VMs that want the
standard browser-tab sort dialog opt in by mixing this in.

Inheritance convention
----------------------
Only the leaf VM that actually drives a ``SortDialog`` should mix this in — never an
intermediate base. The mixin adds no state of its own and inherits ``ViewModelBase.__init__``
unchanged, so adding it to an MRO with another ``ViewModelBase`` ancestor (via cooperative
``super().__init__()``) is safe.

The mixin is generic on the concrete sort-key type ``SortKey``: concrete VMs typically narrow
it to their own ``Literal[...]`` (e.g. ``SortableViewModelMixin[EntrySortKey]``) so static
analysis catches typos at call sites without the widget itself needing to know the alphabet.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Generic, Literal, TypeVar

from ...view_model_base import ViewModelBase

SortKey = TypeVar("SortKey", bound=str)
SortDirection = Literal["asc", "desc"]


class SortableViewModelMixin(ViewModelBase, Generic[SortKey]):
    """Marker mixin for VMs whose state includes a sort axis + direction the user can change
    via a ``SortDialog``. Concrete VMs declare the surfaced axes via ``sort_options()`` and
    implement ``set_sort()`` to apply a chosen one.

    Generic on the sort-key type so concrete VMs can narrow to their own ``Literal[...]``
    alphabet. The widget only cares that the values flow correctly between the four members
    listed below.
    """

    @abstractmethod
    def sort_options(self) -> tuple[SortKey, ...]:
        """The sort axes this VM supports, in display order (left-to-right in the dialog).
        The first option doubles as the reset target — the dialog's ``r`` action restores
        ``(sort_options()[0], 'asc')``."""

    @property
    @abstractmethod
    def sort_by(self) -> SortKey:
        """Currently active sort axis."""

    @property
    @abstractmethod
    def sort_dir(self) -> SortDirection:
        """Currently active sort direction."""

    @abstractmethod
    def set_sort(self, sort_by: SortKey, sort_dir: SortDirection) -> None:
        """Apply a new sort axis + direction. The dialog calls this on ``enter`` (toggle
        direction when on the active axis; switch axis + go ascending otherwise) and on ``r``
        (reset to the first option ascending)."""
