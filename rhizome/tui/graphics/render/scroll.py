"""``ScrollImage`` — a pan/zoomable image with a pinned cell footprint, inside a Textual ``ScrollView``.

The fit-whole ``Image`` re-fits its box on every resize, so the picture never overflows. ``ScrollImage``
does the opposite: the fit computed at first layout is the zoom-1.0 baseline, and ``set_zoom`` scales that
footprint, growing ``virtual_size`` past the viewport so ScrollView's machinery (scrollbars, wheel, arrow
keys) pans around the image.

A terminal font-zoom (the cell px change, distinct from a pure window resize that only re-grids) is handled
per ``zoom_tracks_font``: by default the image's *physical size* is held (the canvas px stay put, only the
cell footprint re-flows), so zoom stays entirely under ``set_zoom``/the ``+``-``-`` keys; with it ``True`` a
font-zoom instead drives the image zoom — the footprint is held in cells, so growing cells grow the canvas,
and ``set_zoom`` composes on top.

The structural consequence of scrolling: a sixel blob can't be partially drawn (no clip rectangle, no
negative cursor rows), so **every scroll offset needs its own encode of the viewport-sized window** cropped
from the zoomed canvas. Windows encode off-thread, one at a time with latest-wins (offsets crossed
mid-burst are skipped, the trailing edge wins) and land in a small LRU so revisited offsets are free. A
paint whose window isn't ready re-emits the previous blob ("stale-until-ready"): the compositor diffs
strips, so an unchanged stale blob costs zero bytes and the image holds still until the fresh window snaps
in. Per-window cost is viewport-bound, not canvas-bound — zooming deeper never makes scrolling slower.

Three placement rules keep the blob in its lane: it is sized to ``scrollable_content_region`` (never the
full widget region) so it can't paint over the scrollbars; it rides the last *content* row's strip (every
covered cell row must be written before the blob, or its background would overwrite the pixels); and the
strip is built by ``blob_strip`` (the cut-survival shape). Same-screen floats can't be composed under, so
the blob is suppressed while anything overlaps (``first_occluder``).

NOTE: unlike the fit-whole widgets, ``ScrollImage`` talks to the sixel encoder directly (``_encode_sixel``
+ ``blob_strip``) rather than through ``GraphicsBackend`` — its window-crop model doesn't fit the
letterbox-the-whole-page ``encode``/``compose`` contract. It is sixel-specialized for now; a kitty/TGP
window path would be added alongside when that backend lands. It takes a ready bitmap (``show(bitmap)``);
off-thread *initial* rasterization (the ``ImageSource`` story) is a deliberate later addition.
"""

import math
from collections import OrderedDict
from functools import partial

from PIL import Image as PILImage
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.dom import NoScreen
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.worker import WorkerState

from rhizome.tui.graphics.environment import active_backend, cell_metrics, unavailable_reason
from rhizome.tui.graphics.render.image import first_occluder
from rhizome.tui.graphics.render.sixel import SixelOptions, _encode_sixel, blob_strip

ZOOM_STEP = 1.25            # multiplicative zoom per set_zoom step (a convenience for callers)
ZOOM_MIN, ZOOM_MAX = 1.0, 8.0
_WINDOW_CACHE_MAX = 32      # encoded windows kept; ~one screenful of sixel bytes each


def _encode_window(canvas: PILImage.Image, box: tuple, options: SixelOptions, background: tuple,
                   key: tuple) -> tuple:
    """Worker body: crop the canvas to ``box`` (canvas px) and sixel-encode it. ``key`` rides along so the
    main thread can tell which window (and which canvas epoch) the result belongs to."""
    return key, _encode_sixel(canvas.crop(box), options, background)


class ScrollImage(ScrollView):
    """A pan/zoomable image with a pinned cell footprint, scrolled by window-cropped sixel re-encodes."""

    DEFAULT_CSS = "ScrollImage { width: 1fr; height: 1fr; }"

    def __init__(self, options: SixelOptions = SixelOptions(), *, zoom_tracks_font: bool = False,
                 name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._options = options
        self._zoom_tracks_font = zoom_tracks_font   # a font-zoom drives image zoom (True) or is absorbed (False)
        self._bitmap = None
        self._zoom = 1.0
        self._fit = None                            # zoom-1.0 scale (image px -> screen px), pinned once
        self._canvas = None                         # the image resized for the current zoom
        self._built_cell = None                     # cell size the canvas was last built at — font-zoom detector
        self._epoch = 0                             # bumped per canvas rebuild; stamps every window key
        self._windows: "OrderedDict[tuple, str]" = OrderedDict()   # (epoch, window) -> sixel
        self._stale = None                          # (window_size, sixel) of the last blob actually shown
        self._inflight = None                       # the single in-progress encode worker, or None
        self._wanted = None                         # (key, window) most recently missed — latest wins
        self._occluded = False                      # a same-screen float overlaps us right now

    # -- public API --------------------------------------------------------------------------------

    def show(self, bitmap) -> None:
        """Display ``bitmap`` at zoom 1.0 (re-pins the footprint at the next layout)."""
        self._bitmap = bitmap
        self._zoom = 1.0
        self._fit = self._canvas = self._wanted = None
        self._windows.clear()
        self._pin_and_build()                       # pins now if we already have a size; else on_resize does
        self.refresh()

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        """Re-pin the footprint at ``zoom`` x the first-layout fit, keeping the viewport center anchored."""
        zoom = min(ZOOM_MAX, max(ZOOM_MIN, zoom))
        if self._fit is None or zoom == self._zoom:
            return
        vw, vh = self.scrollable_size
        factor = zoom / self._zoom
        cx = (self.scroll_offset.x + vw / 2) * factor - vw / 2
        cy = (self.scroll_offset.y + vh / 2) * factor - vh / 2
        self._zoom = zoom
        self._rebuild_canvas()
        self.scroll_to(max(0, cx), max(0, cy), animate=False, force=True)
        self.refresh()

    # -- zoom / canvas -----------------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        """First layout pins the zoom-1.0 fit. Afterwards a *pure window resize* keeps the footprint (the
        point of the widget); a *font-zoom* (the cell px changed) either holds the image's physical size or
        drives the zoom, per ``zoom_tracks_font``."""
        if self._fit is None:
            self._pin_and_build()
            return
        cell = cell_metrics().current
        if self._built_cell is None or cell == self._built_cell:
            return                                  # window resize only: footprint stays pinned, window re-crops
        if self._zoom_tracks_font:
            self._fit *= cell.height / self._built_cell.height   # bigger cells -> bigger canvas -> tracks font
            self._rebuild_canvas()
        else:
            self._reflow_to_cell(cell)              # hold canvas px (physical size); just re-cell the footprint
        self.refresh()

    def _reflow_to_cell(self, cell) -> None:
        """Font-zoom, exogenous-zoom mode: keep the canvas px so the image holds its physical size, and just
        re-derive ``virtual_size`` for the new cell. The cell px changed, so cached windows are retired."""
        self._epoch += 1
        self._windows.clear()
        self._wanted = None
        self.virtual_size = Size(math.ceil(self._canvas.width / cell.width),
                                 math.ceil(self._canvas.height / cell.height))
        self._built_cell = cell

    def _pin_and_build(self) -> None:
        if self._bitmap is None or self.scrollable_size.width <= 0 or self.scrollable_size.height <= 0:
            return
        cell = cell_metrics().current
        box_w, box_h = self.scrollable_size.width * cell.width, self.scrollable_size.height * cell.height
        self._fit = min(box_w / self._bitmap.width, box_h / self._bitmap.height)
        self._rebuild_canvas()

    def _rebuild_canvas(self) -> None:
        """Resize the image to footprint x zoom. A rebuild orphans every cached/in-flight window encode."""
        cell = cell_metrics().current
        scale = self._fit * self._zoom
        w, h = max(1, round(self._bitmap.width * scale)), max(1, round(self._bitmap.height * scale))
        self._canvas = self._bitmap.resize((w, h))
        self._built_cell = cell
        self._epoch += 1
        self._windows.clear()
        self._wanted = None
        self.virtual_size = Size(math.ceil(w / cell.width), math.ceil(h / cell.height))

    # -- rendering ---------------------------------------------------------------------------------

    def render_lines(self, crop: Region) -> list[Strip]:
        try:
            if active_backend() is None:
                reason = unavailable_reason()
                return self._notice(crop, reason.value if reason else "terminal graphics unavailable")
            if self._canvas is None or not self.screen.is_active:
                return self._notice(crop)
        except NoScreen:
            return self._notice(crop)
        if self._visible_region() is None:
            return self._notice(crop)

        occluded = first_occluder(self) is not None
        if occluded != self._occluded:              # a float arrived or left: repaint the whole image area
            self._occluded = occluded
            self.refresh()
        if occluded:
            return self._notice(crop)

        cell = cell_metrics().current
        vw, vh = self.scrollable_size
        if vw <= 0 or vh <= 0:
            return self._notice(crop)

        # The window is keyed by scroll offset + viewport, never the paint crop — a 1-row style probe maps
        # to the same key as a full paint, so probes can't thrash the cache or trigger encodes.
        window = Region(self.scroll_offset.x, self.scroll_offset.y, vw, vh)
        key = (self._epoch, window)
        sixel = self._windows.get(key)
        if sixel is not None:
            self._windows.move_to_end(key)
            self._stale = (window.size, sixel)
        else:
            self._request_window(key, window, cell)
            if self._stale is None or self._stale[0] != window.size:
                return self._notice(crop, "rendering…")
            sixel = self._stale[1]                   # stale-until-ready: hold the last blob, no flash

        # Blit at the content origin, plus centering slack when the canvas underfills the viewport.
        content = self.scrollable_content_region
        ox = max(0, (vw - self.virtual_size.width) // 2)
        oy = max(0, (vh - self.virtual_size.height) // 2)
        blobs = [(sixel, content.x + ox, content.y + oy)]
        blobs += self._overlay_blobs(window, content.x + ox - window.x, content.y + oy - window.y)
        return self._window_strips(blobs, vw, vh, crop, self._pad_style(window, cell))

    def _overlay_blobs(self, window: Region, base_x: int, base_y: int) -> list[tuple]:
        """Subclass hook: extra ``(sixel, x, y)`` blobs blitted after the page window (drawn on top).

        ``window`` is the visible canvas-cell window; ``base`` maps canvas cells to screen cells
        (screen = base + canvas_cell), so canvas-absolute overlays just add it. Overlays must already be
        clipped to ``window`` — pixels outside the viewport paint over scrollbars and siblings.
        """
        return []

    def _window_strips(self, blobs: list[tuple], content_cols: int, content_rows: int,
                       crop: Region, pad_style: Style) -> list[Strip]:
        """Background-clear strips with the blobs riding the last *content* row (above any h-scrollbar)."""
        _, color = self.background_colors
        clear = Segment(" " * crop.width, style=Style(bgcolor=color.rich_color))
        lines = [Strip([clear], cell_length=crop.width) for _ in range(crop.height)]
        blob_row = content_rows - 1 - crop.y
        if 0 <= blob_row < len(lines):
            lines[blob_row] = blob_strip(blobs, content_cols, Style(bgcolor=color.rich_color), pad_style)
        return lines

    def _pad_style(self, window: Region, cell) -> Style:
        """Mean canvas color under the viewport's bottom-right cell (the pad cell's disguise)."""
        px, py = (window.right - 1) * cell.width, (window.bottom - 1) * cell.height
        box = (px, py, min(self._canvas.width, px + cell.width), min(self._canvas.height, py + cell.height))
        _, color = self.background_colors
        if box[2] <= box[0] or box[3] <= box[1]:
            return Style(bgcolor=color.rich_color)
        r, g, b = self._canvas.crop(box).resize((1, 1), PILImage.Resampling.BOX).getpixel((0, 0))[:3]
        return Style(bgcolor=Color.from_rgb(r, g, b))

    # -- the one-at-a-time, latest-wins encode pipeline --------------------------------------------

    def _request_window(self, key: tuple, window: Region, cell) -> None:
        self._wanted = (key, window)
        if self._inflight is None:
            self._dispatch(key, window, cell)

    def _dispatch(self, key: tuple, window: Region, cell) -> None:
        box = (window.x * cell.width, window.y * cell.height,
               min(self._canvas.width, window.right * cell.width),
               min(self._canvas.height, window.bottom * cell.height))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        self._inflight = self.run_worker(
            partial(_encode_window, self._canvas, box, self._options, self._background_rgba(), key),
            thread=True, description="encode scroll window", exit_on_error=False)

    def on_worker_state_changed(self, event) -> None:
        if event.worker is not self._inflight:
            return                                  # a pre-rebuild encode landing late — drop it
        if event.state is WorkerState.SUCCESS:
            key, sixel = event.worker.result
            self._inflight = None
            if key[0] == self._epoch:               # an old-epoch result is orphaned, not installed
                self._windows[key] = sixel
                while len(self._windows) > _WINDOW_CACHE_MAX:
                    self._windows.popitem(last=False)
            self._maybe_dispatch_wanted()
            self.refresh()
        elif event.state is WorkerState.ERROR:
            self.log.warning(f"scroll window encode failed: {event.worker.error!r}")
            self._inflight = None
            self._maybe_dispatch_wanted()

    def _maybe_dispatch_wanted(self) -> None:
        """Chain to the most recently missed window — the latest-wins half of the pipeline."""
        if self._wanted is None:
            return
        key, window = self._wanted
        self._wanted = None
        if key[0] == self._epoch and key not in self._windows:
            self._dispatch(key, window, cell_metrics().current)

    # -- internals ---------------------------------------------------------------------------------

    def _background_rgba(self) -> tuple:
        _, color = self.background_colors
        return (color.r, color.g, color.b, color.a)

    def _visible_region(self) -> Region | None:
        try:
            return self.screen.find_widget(self).visible_region
        except (NoScreen, KeyError):
            return None

    def _notice(self, crop: Region, text: str = "") -> list[Strip]:
        _, color = self.background_colors
        fill = Style(bgcolor=color.rich_color)
        lines = [Strip([Segment(" " * crop.width, fill)], cell_length=crop.width) for _ in range(crop.height)]
        if text and crop.height and crop.width:
            label = f" {text} "[:crop.width]
            tail = Segment(" " * (crop.width - len(label)), fill)
            lines[0] = Strip([Segment(label, fill + Style(italic=True)), tail], cell_length=crop.width)
        return lines
