"""Terminal graphics primitives: draw a bitmap (with hover-selectable overlay boxes) in the terminal.

Content-agnostic by design — these know nothing of PDFs or resources. A caller hands a backend a
bitmap plus box rectangles (image px) and the backend renders them through one terminal graphics
protocol. See ``CONTEXT.md`` for the source -> geometry -> backend layering.
"""

from rhizome.tui.graphics.backend import GraphicsBackend, select_backend
from rhizome.tui.graphics.geometry import Placement, first_hit, footprint, placement
from rhizome.tui.graphics.sixel import SixelBackend, SixelOptions
from rhizome.tui.graphics.widget import GraphicsImage

__all__ = [
    "GraphicsImage",
    "GraphicsBackend",
    "select_backend",
    "Placement",
    "placement",
    "footprint",
    "first_hit",
    "SixelBackend",
    "SixelOptions",
]
