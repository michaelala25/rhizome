"""SortableViewModelMixin — the narrow VM contract a ``SortDialog`` consumes.

Four members: ``sort_options()`` (axes in display order; first doubles as the reset target),
``sort_by`` / ``sort_dir`` (current state), and ``set_sort(sort_by, sort_dir)`` (apply).

Generic on the sort-key type so concrete VMs narrow to their own ``Literal[...]`` alphabet
(e.g. ``SortableViewModelMixin[EntrySortKey]``); the widget stays alphabet-agnostic.

Mix this in only at the **leaf** VM that actually drives a dialog — never on a shared base.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Generic, Literal, TypeVar

from rhizome.app.vm import ViewModelBase

SortKey = TypeVar("SortKey", bound=str)
SortDirection = Literal["asc", "desc"]


class SortableViewModelMixin(ViewModelBase, Generic[SortKey]):
    """VM contract for sort-axis selection via ``SortDialog``."""

    @abstractmethod
    def sort_options(self) -> tuple[SortKey, ...]:
        """Surfaced axes in display order. ``[0]`` is the reset target (``r`` in the dialog)."""

    @property
    @abstractmethod
    def sort_by(self) -> SortKey: ...

    @property
    @abstractmethod
    def sort_dir(self) -> SortDirection: ...

    @abstractmethod
    def set_sort(self, sort_by: SortKey, sort_dir: SortDirection) -> None: ...
