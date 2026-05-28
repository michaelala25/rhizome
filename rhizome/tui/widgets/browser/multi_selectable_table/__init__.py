"""Multi-select scaffolding for browser-tab tables: widget + VM mixin."""

from .view import MultiSelectableDataTable
from .view_model_mixin import MultiSelectableVMMixin

__all__ = ["MultiSelectableDataTable", "MultiSelectableVMMixin"]
