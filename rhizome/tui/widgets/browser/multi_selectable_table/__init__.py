"""Multi-select scaffolding for browser-tab tables: widget + VM mixin."""

from .view import MultiSelectableDataTable
from .view_model_mixin import MultiSelectableViewModelMixin

__all__ = ["MultiSelectableDataTable", "MultiSelectableViewModelMixin"]
