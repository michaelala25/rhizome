"""``ScrollSelectableImage`` — a pan/zoomable page with drag-selectable text. The two axes married.

``ScrollImage`` brings the pinned-footprint scroll/zoom + viewport-window encode; ``SelectionModel`` brings
the anchor→focus word range merged into per-line tint bars. The marriage is purely a matter of which
coordinate space each part lives in:

- **Selection state is image px** (word indices), inherited from ``SelectionModel`` — zoom and scroll
  never touch it, so a selection survives both.
- **Tiles are encoded in canvas-absolute cells** (cell-aligned crops of the *zoomed canvas*). A tile
  encoded once is valid at every scroll offset; per paint it only needs *clipping* to the viewport window
  — a run straddling the edge gets a clipped variant, cached separately. Zoom bumps the canvas epoch,
  which retires every tile key at once.
- **Blitting adds the base offset** ``ScrollImage`` hands ``_overlay_blobs`` (screen cell = canvas cell -
  scroll + centering slack); tiles ride the same ``blob_strip`` as the page, after it, so they draw on top.

The hit-test inverts the same transform: viewport cell → canvas cell (add scroll, drop slack) → image px.
Like the page, a selection painted over a stale mid-scroll window is transiently misplaced and self-heals
on the snap. Sixel-specialized, for the same reason ``ScrollImage`` is.
"""

import math
from collections import OrderedDict

from PIL import Image as PILImage
from textual.geometry import Region
from textual.message import Message

from rhizome.tui.graphics.environment import cell_metrics
from rhizome.tui.graphics.render.backend import Fill
from rhizome.tui.graphics.render.geometry import first_hit
from rhizome.tui.graphics.render.image import Throttle
from rhizome.tui.graphics.render.scroll import ScrollImage
from rhizome.tui.graphics.render.select import SelectionModel, Word
from rhizome.tui.graphics.render.sixel import SixelHighlight, _encode_sixel

_TILES_MAX = 256            # cached tint tiles (full + clipped variants); each a line-run sized blob


class ScrollSelectableImage(SelectionModel, Throttle, ScrollImage):
    """A pan/zoomable page whose words can be drag-selected; tiles tint the zoomed canvas, clipped to view."""

    class SelectionChanged(Message):
        """The selection changed — ``text`` is the selected words joined by spaces, ``count`` how many."""

        def __init__(self, text: str, count: int) -> None:
            super().__init__()
            self.text = text
            self.count = count

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._init_selection()
        self._words: list[Word] = []
        self._fill = Fill()
        self._tiles: "OrderedDict[tuple, SixelHighlight | None]" = OrderedDict()

    def show(self, bitmap, words=()) -> None:
        """Display ``bitmap`` (full-res page) with its selectable ``Word`` list, at zoom 1.0."""
        self._words = list(words)
        self._anchor = self._focus = None
        self._tiles.clear()
        super().show(bitmap)

    # -- hit-test: viewport cell -> canvas cell -> image px --------------------------------------

    def _word_at(self, cell_x: int, cell_y: int) -> int | None:
        if self._canvas is None or self._fit is None:
            return None
        cell = cell_metrics().current
        vw, vh = self.scrollable_size
        ccx = cell_x + self.scroll_offset.x - max(0, (vw - self.virtual_size.width) // 2)
        ccy = cell_y + self.scroll_offset.y - max(0, (vh - self.virtual_size.height) // 2)
        scale = self._fit * self._zoom
        footprint = (ccx * cell.width / scale, ccy * cell.height / scale,
                     (ccx + 1) * cell.width / scale, (ccy + 1) * cell.height / scale)
        return first_hit(footprint, [w.rect for w in self._words])

    # -- tiles: canvas-absolute cells, clipped to the window per paint ---------------------------

    def _overlay_blobs(self, window: Region, base_x: int, base_y: int) -> list[tuple]:
        blobs = []
        for run in self._selection_runs():
            tile = self._tile_for(run, window)
            if tile is not None:
                blobs.append((tile.sixel, base_x + tile.cell_x, base_y + tile.cell_y))
        return blobs

    def _tile_for(self, rect: tuple, window: Region):
        """Get-or-encode a run's tile clipped to ``window`` — keyed by (epoch, run, clip) so it's reused."""
        cell = cell_metrics().current
        scale = self._fit * self._zoom
        cx0 = max(0, int(rect[0] * scale // cell.width))
        cy0 = max(0, int(rect[1] * scale // cell.height))
        cx1, cy1 = math.ceil(rect[2] * scale / cell.width), math.ceil(rect[3] * scale / cell.height)
        visible = Region.from_corners(cx0, cy0, cx1, cy1).intersection(window)
        if visible.width <= 0 or visible.height <= 0:
            return None

        key = (self._epoch, rect, visible)                    # epoch retires every tile on a zoom rebuild
        if key not in self._tiles:
            self._tiles[key] = self._encode_tile(visible, cell)
            while len(self._tiles) > _TILES_MAX:
                self._tiles.popitem(last=False)
        self._tiles.move_to_end(key)
        return self._tiles[key]

    def _encode_tile(self, cells: Region, cell):
        """A uniform translucent wash over the cell region's crop of the clean canvas — one tile."""
        box = (cells.x * cell.width, cells.y * cell.height,
               min(self._canvas.width, cells.right * cell.width),
               min(self._canvas.height, cells.bottom * cell.height))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        crop = self._canvas.crop(box)
        tinted = PILImage.blend(crop, PILImage.new("RGB", crop.size, self._fill.color), self._fill.alpha)
        return SixelHighlight(cells.x, cells.y, _encode_sixel(tinted, self._options, self._background_rgba()))
