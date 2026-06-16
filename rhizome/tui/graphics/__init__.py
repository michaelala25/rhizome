"""Terminal graphics: draw a bitmap (with hover-selectable overlay boxes) in the terminal.

Content-agnostic by design — nothing here knows about PDFs or resources. A caller hands a backend a
bitmap plus box rectangles (image px) and the backend renders them through one terminal graphics
protocol. See ``DOC.md`` for the terminal mechanics (the query "dance", cell-size detection, sixel,
coordinate frames) and ``CONTEXT.md`` for the layering.

Usage contract:

    import rhizome.tui.graphics as graphics

    graphics.initialize()                 # ONCE, in the app entry point, BEFORE the Textual app runs
    ...
    img = graphics.Image()                # reads the selected backend from the environment
    img.show(bitmap)                      # or graphics.ImageWithOverlays().show(bitmap, regions)

    # in the app's on_resize, to keep the cell size live:
    graphics.note_resize(self.size.width, self.size.height, event.pixel_size)
"""

from rhizome.tui.graphics.environment import (
    GraphicsEnvironment, active_backend, cell_metrics, environment, initialize, note_resize,
    unavailable_reason)
from rhizome.tui.graphics.render.backend import Fill, GraphicsBackend, Outline
from rhizome.tui.graphics.render.geometry import Placement, first_hit, footprint, placement
from rhizome.tui.graphics.render.image import Image, first_occluder
from rhizome.tui.graphics.render.overlays import ImageWithOverlays
from rhizome.tui.graphics.render.scroll import ScrollImage
from rhizome.tui.graphics.render.scroll_select import ScrollSelectableImage
from rhizome.tui.graphics.render.select import SelectableImage, Word
from rhizome.tui.graphics.render.sixel import SixelBackend, SixelOptions
from rhizome.tui.graphics.render.source import ImageSource, RenderContext, StaticSource
from rhizome.tui.graphics.terminal.capabilities import GraphicsUnavailable
from rhizome.tui.graphics.terminal.cellsize import CellMetrics, CellSize

__all__ = [
    # lifecycle / environment
    "initialize",
    "note_resize",
    "environment",
    "GraphicsEnvironment",
    "active_backend",
    "unavailable_reason",
    "cell_metrics",
    "GraphicsUnavailable",
    "CellSize",
    "CellMetrics",
    # widgets
    "Image",
    "ImageWithOverlays",
    "SelectableImage",
    "ScrollImage",
    "ScrollSelectableImage",
    "Word",
    "first_occluder",
    # overlay styles
    "Outline",
    "Fill",
    # sources (off-thread rasterization)
    "ImageSource",
    "RenderContext",
    "StaticSource",
    # backends
    "GraphicsBackend",
    "SixelBackend",
    "SixelOptions",
    # geometry
    "Placement",
    "placement",
    "footprint",
    "first_hit",
]
