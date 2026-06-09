"""``GraphicsImage`` — a reusable widget that draws a bitmap with hover-selectable regions.

Content-agnostic: hand it a bitmap and a list of ``(rect, payload)`` regions (rects in image px,
payloads opaque) and it renders them through a ``GraphicsBackend``, posting ``RegionHovered`` /
``RegionSelected`` as the mouse moves and clicks. The text echoing, page navigation, copy-to-clipboard
etc. belong to whatever mounts this and interprets the payloads.

How the off-thread render works (the heart of the widget):

- A paint computes an ``EncodeJob`` from the current bitmap + layout (``placement``) and looks it up
  in ``_frames``, a bounded ``dict[EncodeJob, Worker]``. The job is **crop-independent**, so a real
  paint and a ``get_style_at`` probe map to the same job — the cache never thrashes between them.
- On a miss ``_ensure_frame`` dispatches one worker to ``backend.encode`` (idempotent: a job already
  in flight is reused, never re-dispatched) and the paint shows a "rendering…" placeholder. The heavy
  encode never runs on the event loop.
- Once a frame lands, ``_ensure_overlays`` pre-encodes *every* region's hover overlay on a second
  worker, so hovering is a pure cache hit. A region hovered before that finishes falls back to a cheap
  synchronous encode — the only main-thread encode, and a rare one.
- When a worker resolves, ``on_worker_state_changed`` calls ``refresh()``; the repaint re-enters
  ``render_lines`` as a cache hit. ``prefetch`` is the frame dispatch made eagerly for a neighbour.

Hover repaints are coalesced to at most one per ``_HOVER_REPAINT_INTERVAL`` (latest-wins): a sixel
draws slower than the mouse crosses regions, so without a cap each crossed region queues a full-frame
re-emit and the outline trails the cursor. A region crossed mid-cooldown is dropped and the trailing
edge paints wherever the cursor actually landed. The cheap ``RegionHovered`` echo is never throttled.

Detection note: a backend must be chosen *before* the Textual app starts (it queries the terminal).
Pass ``select_backend()``'s result in; with ``backend=None`` the widget shows an "unavailable" notice.

Caveat for owners: a sixel touching the screen's last row makes the terminal scroll on re-emit —
reserve a bottom row so this widget never reaches it.
"""

from collections import OrderedDict
from functools import partial

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.dom import NoScreen
from textual.geometry import Region
from textual.message import Message
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.worker import Worker, WorkerState
from textual_image._terminal import get_cell_size

from rhizome.tui.graphics.backend import GraphicsBackend
from rhizome.tui.graphics.geometry import first_hit, footprint, placement

_FRAMES_MAX = 8                         # encoded frames kept (current + neighbours); overlays follow them
_FRAME_GROUP = "graphics-frame"
_OVERLAY_GROUP = "graphics-overlays"
_HOVER_REPAINT_INTERVAL = 0.06          # min seconds between hover repaints; mid-cooldown hovers are dropped


class GraphicsImage(Widget):
    """Draw a bitmap with hover-selectable regions through a ``GraphicsBackend``."""

    DEFAULT_CSS = """
    GraphicsImage { width: 1fr; height: 1fr; }
    """

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

    def __init__(self, backend: GraphicsBackend | None = None, *, halign: str = "center",
                 name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._backend = backend
        self._halign = halign
        self._bitmap = None
        self._regions: list[tuple] = []                       # (rect, payload)
        self._hovered: int | None = None
        self._frames: "OrderedDict[object, Worker]" = OrderedDict()       # job -> page-encode worker
        self._overlay_workers: "OrderedDict[object, Worker]" = OrderedDict()  # job -> prime-all worker
        self._overlays: dict[tuple, object] = {}              # (job, rect) -> Highlight | None
        self._repaint_timer: Timer | None = None              # throttles hover repaints (None = idle)
        self._repaint_dirty = False                           # hover changed during the cooldown window

    # -- public API ------------------------------------------------------------------------------

    def show(self, bitmap, regions=()) -> None:
        """Display ``bitmap`` and its ``(rect, payload)`` regions. Encoding happens at the next paint."""
        self._bitmap = bitmap
        self._regions = list(regions)
        self._hovered = None
        self.refresh()

    def prefetch(self, bitmap) -> None:
        """Warm the frame cache for ``bitmap`` at the current layout — for an owner's neighbour prefetch."""
        job = self._job_for(bitmap)
        if job is not None:
            self._ensure_frame(job)

    @property
    def hovered(self) -> int | None:
        return self._hovered

    # -- rendering -------------------------------------------------------------------------------

    def render_lines(self, crop: Region) -> list[Strip]:
        try:
            if self._backend is None:
                return self._notice(crop, "terminal graphics unavailable")
            if self._bitmap is None or not self.screen.is_active:
                return self._notice(crop)
        except NoScreen:
            return self._notice(crop)

        job = self._job_for(self._bitmap)
        region = self._visible_region()
        if job is None or region is None:
            return self._notice(crop)

        frame_worker = self._ensure_frame(job)
        if frame_worker.state is not WorkerState.SUCCESS:
            return self._notice(crop, "rendering…")
        frame = frame_worker.result

        self._ensure_overlays(job, frame)                     # pre-encode all overlays off-thread, once
        highlight = None
        if self._hovered is not None:
            highlight = self._overlay(job, frame, self._regions[self._hovered][0])
        return self._backend.compose(frame, highlight, crop, region=region)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS:
            if event.worker.group == _OVERLAY_GROUP:          # a prime-all finished: install its overlays
                job, encoded = event.worker.result
                self._overlays.update({(job, rect): hl for rect, hl in encoded})
            self.refresh()                                    # a frame/overlay landed -> repaint hits the cache
        elif event.state is WorkerState.ERROR:
            self.log.warning(f"graphics encode failed: {event.worker.error!r}")

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

    def _request_repaint(self) -> None:
        """Coalesce hover repaints to <=1 per ``_HOVER_REPAINT_INTERVAL``, latest-wins.

        While a repaint cools down we only mark the frame dirty, so every region the cursor crosses
        mid-cooldown is abandoned; the trailing edge paints whichever region it has actually reached.
        """
        if self._repaint_timer is not None:
            self._repaint_dirty = True
            return
        self.refresh()                                          # leading edge: paint the live hover now
        self._repaint_dirty = False
        self._repaint_timer = self.set_timer(_HOVER_REPAINT_INTERVAL, self._on_repaint_cooldown)

    def _on_repaint_cooldown(self) -> None:
        self._repaint_timer = None
        if self._repaint_dirty:                                 # cursor moved during cooldown -> paint latest
            self._request_repaint()

    # -- internals -------------------------------------------------------------------------------

    def _ensure_frame(self, job) -> Worker:
        """Get-or-dispatch the encode worker for ``job`` (idempotent — in-flight jobs are reused)."""
        worker = self._frames.get(job)
        if worker is None or worker.state in (WorkerState.CANCELLED, WorkerState.ERROR):
            worker = self.run_worker(partial(self._backend.encode, job), thread=True,
                                     group=_FRAME_GROUP, description="encode graphics frame",
                                     exit_on_error=False)
            self._frames[job] = worker
            while len(self._frames) > _FRAMES_MAX:
                evicted_job, evicted = self._frames.popitem(last=False)
                evicted.cancel()
                self._forget(evicted_job)                     # overlays live exactly as long as their frame
        self._frames.move_to_end(job)
        return worker

    def _ensure_overlays(self, job, frame) -> None:
        """Dispatch one worker to pre-encode every region's overlay for ``frame`` — once per job."""
        if not self._regions or job in self._overlay_workers:
            return
        rects = [rect for rect, _payload in self._regions]
        self._overlay_workers[job] = self.run_worker(
            partial(self._encode_overlays, job, frame, rects), thread=True,
            group=_OVERLAY_GROUP, description="pre-encode hover overlays", exit_on_error=False)

    def _encode_overlays(self, job, frame, rects) -> tuple:
        """Worker body: encode every region's overlay. Pure — results are installed on the main thread."""
        return job, [(rect, self._backend.encode_highlight(frame, rect)) for rect in rects]

    def _overlay(self, job, frame, rect):
        """The hover overlay for ``rect`` — a cache hit once primed, else a cheap synchronous fallback."""
        key = (job, rect)
        if key not in self._overlays:
            self._overlays[key] = self._backend.encode_highlight(frame, rect)
        return self._overlays[key]

    def _forget(self, job) -> None:
        """Drop everything tied to an evicted frame: its prime worker and its overlays."""
        worker = self._overlay_workers.pop(job, None)
        if worker is not None:
            worker.cancel()
        for key in [k for k in self._overlays if k[0] == job]:
            del self._overlays[key]

    def _job_for(self, bitmap):
        """An ``EncodeJob`` for ``bitmap`` at the current layout, or None if not yet laid out."""
        if bitmap is None or self._backend is None:
            return None
        cell = get_cell_size()
        box_w, box_h = self.content_size.width * cell.width, self.content_size.height * cell.height
        if box_w <= 0 or box_h <= 0:
            return None
        place = placement(bitmap.width, bitmap.height, box_w, box_h, self._halign)
        return self._backend.prepare(bitmap, place, cell, background=self._background_rgba())

    def _region_at(self, cell_x: int, cell_y: int) -> int | None:
        """Hit-test a widget cell against the regions, via the same ``placement`` used to render."""
        if self._bitmap is None or not self._regions:
            return None
        cell = get_cell_size()
        box_w, box_h = self.content_size.width * cell.width, self.content_size.height * cell.height
        if box_w <= 0 or box_h <= 0:
            return None
        place = placement(self._bitmap.width, self._bitmap.height, box_w, box_h, self._halign)
        fp = footprint(place, cell_x, cell_y, cell.width, cell.height)
        return first_hit(fp, [rect for rect, _payload in self._regions])

    def _visible_region(self) -> Region | None:
        try:
            return self.screen.find_widget(self).visible_region
        except (NoScreen, KeyError):
            return None

    def _background_rgba(self) -> tuple:
        _, color = self.background_colors
        return (color.r, color.g, color.b, color.a)

    def _notice(self, crop: Region, text: str = "") -> list[Strip]:
        """A blank, background-filled paint, optionally with a dim label on the first row."""
        _, color = self.background_colors
        fill = Style(bgcolor=color.rich_color)
        lines = [Strip([Segment(" " * crop.width, fill)], cell_length=crop.width) for _ in range(crop.height)]
        if text and crop.height and crop.width:
            label = f" {text} "[:crop.width]
            tail = Segment(" " * (crop.width - len(label)), fill)
            lines[0] = Strip([Segment(label, fill + Style(italic=True)), tail], cell_length=crop.width)
        return lines
