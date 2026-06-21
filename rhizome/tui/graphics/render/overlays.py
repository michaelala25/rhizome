"""``ImageWithOverlays`` — an ``Image`` with hover-selectable regions drawn on top of the page.

Hand it a bitmap (or source) and a list of ``(rect, payload)`` regions (rects in image px, payloads
opaque). It hit-tests the mouse against the regions, outlines the hovered one, and posts ``RegionHovered``
/ ``RegionSelected``. The text echoing, navigation, copy-to-clipboard etc. belong to whatever mounts this
and interprets the payloads.

It reuses ``Image``'s render pipeline and adds the overlay machinery: a cache of encoded overlay tiles
keyed by ``(EncodeJob, rect, style)`` (``_overlay_tile``), an optional pre-encode of *every* region's
tile once a frame lands (so hovering is a pure cache hit), and the ``_overlays_for`` hook the base blits.
``SelectableImage`` reuses the same tile cache and hit-test with a ``Fill`` style instead of ``Outline``;
the ``Throttle`` mixin (shared with the selection widgets) coalesces the hover repaints.
"""

from collections import OrderedDict
from functools import partial

from textual import events
from textual.message import Message
from textual.worker import Worker, WorkerState

from rhizome.tui.graphics.environment import cell_metrics
from rhizome.tui.graphics.render.backend import Outline
from rhizome.tui.graphics.render.geometry import first_hit, footprint, placement
from rhizome.tui.graphics.render.image import Image, Throttle

_OVERLAY_GROUP = "graphics-overlays"


class ImageWithOverlays(Throttle, Image):
    """An ``Image`` whose hovered region is outlined, hit-tested against ``(rect, payload)`` regions."""

    class RegionHovered(Message):
        """The hovered region changed (``index``/``payload`` are None when nothing is hovered)."""

        def __init__(self, index: int | None, payload: object) -> None:
            super().__init__()
            self.index = index
            self.payload = payload

    class RegionSelected(Message):
        """A region was clicked."""

        def __init__(self, index: int, payload: object) -> None:
            super().__init__()
            self.index = index
            self.payload = payload

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._regions: list[tuple] = []                       # (rect, payload)
        self._hovered: int | None = None
        self._overlay_style = Outline()                       # how a tile is painted (subclasses may change)
        self._overlay_workers: "OrderedDict[object, Worker]" = OrderedDict()  # job -> prime-all worker
        self._tiles: dict[tuple, object] = {}                 # (job, rect, style) -> Highlight | None

    # -- public API ------------------------------------------------------------------------------

    def show(self, source, regions=()) -> None:
        """Display ``source`` (a bitmap or ``ImageSource``) and its ``(rect, payload)`` regions."""
        self._regions = list(regions)
        self._hovered = None
        super().show(source)

    @property
    def hovered(self) -> int | None:
        return self._hovered

    # -- overlay hook ----------------------------------------------------------------------------

    def _overlays_for(self, job, frame) -> list:
        self._prime_overlays(job, frame)                      # pre-encode all region tiles off-thread, once
        if self._hovered is None:
            return []
        tile = self._overlay_tile(frame, self._regions[self._hovered][0])
        return [tile] if tile is not None else []

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS and event.worker.group == _OVERLAY_GROUP:
            job, style, encoded = event.worker.result         # a prime-all finished: install its tiles
            self._tiles.update({(job, rect, style): tile for rect, tile in encoded})
        super().on_worker_state_changed(event)                # refresh on success / log on error

    # -- mouse -----------------------------------------------------------------------------------

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_hovered(self._region_at(event.offset.x, event.offset.y))

    def on_leave(self, event: events.Leave) -> None:
        self._set_hovered(None)

    def on_click(self, event: events.Click) -> None:
        index = self._region_at(event.offset.x, event.offset.y)
        if index is not None:
            self.post_message(self.RegionSelected(index, self._regions[index][1]))

    def _set_hovered(self, index: int | None) -> None:
        if index == self._hovered:
            return
        self._hovered = index
        payload = self._regions[index][1] if index is not None else None
        self.post_message(self.RegionHovered(index, payload))   # cheap echo — never throttled
        self._request_repaint()                                 # expensive sixel repaint — throttled

    # -- overlay tiles ---------------------------------------------------------------------------

    def _overlay_tile(self, frame, rect):
        """Get-or-encode one overlay tile for ``rect`` in this widget's style — cached, synchronous.

        Shared by hover (the hovered region) and selection (each per-line run). A cache hit once primed,
        else a cheap synchronous encode. ``Outline`` and ``Fill`` tiles for the same rect cache separately.
        """
        key = (frame.job, rect, self._overlay_style)
        if key not in self._tiles:
            self._tiles[key] = self.backend.encode_highlight(frame, rect, self._overlay_style)
        return self._tiles[key]

    def _prime_overlays(self, job, frame) -> None:
        """Dispatch one worker to pre-encode every region's tile for ``frame`` — once per job (hover only)."""
        if not self._regions or job in self._overlay_workers:
            return
        rects = [rect for rect, _payload in self._regions]
        self._overlay_workers[job] = self.run_worker(
            partial(self._encode_overlays, job, frame, rects, self._overlay_style), thread=True,
            group=_OVERLAY_GROUP, description="pre-encode overlay tiles", exit_on_error=False)

    def _encode_overlays(self, job, frame, rects, style) -> tuple:
        """Worker body: encode every region's tile. Pure — results are installed on the main thread."""
        return job, style, [(rect, self.backend.encode_highlight(frame, rect, style)) for rect in rects]

    def _forget(self, job) -> None:
        """Drop everything tied to an evicted frame: its prime worker and its tiles."""
        worker = self._overlay_workers.pop(job, None)
        if worker is not None:
            worker.cancel()
        for key in [k for k in self._tiles if k[0] == job]:
            del self._tiles[key]

    # -- hit-test --------------------------------------------------------------------------------

    def _region_at(self, cell_x: int, cell_y: int) -> int | None:
        """Hit-test a widget cell against the regions, via the same ``placement`` used to render."""
        if self._bitmap is None or not self._regions:
            return None
        cell = cell_metrics().current
        box_w, box_h = self.content_size.width * cell.width, self.content_size.height * cell.height
        if box_w <= 0 or box_h <= 0:
            return None
        place = placement(self._bitmap.width, self._bitmap.height, box_w, box_h, self._halign,
                          self._max_scale())
        fp = footprint(place, cell_x, cell_y, cell.width, cell.height)
        return first_hit(fp, [rect for rect, _payload in self._regions])
