"""``Image`` — a reusable widget that draws a bitmap in the terminal, encoded off the event loop.

Content-agnostic: hand it a bitmap, *or a source that produces one*, with ``show(...)`` and it renders
through a ``GraphicsBackend``. The backend and cell size come from the process-global ``environment``
(populated once by ``graphics.initialize()``); a widget doesn't have to be handed either. Pass ``backend``
explicitly only to override detection (tests, or forcing a protocol). With no backend available the widget
shows the environment's structured "unavailable" reason.

The off-thread render is a **two-stage pipeline**, each stage cached and run on a worker:

    source ──(rasterize, cached by source.cache_key)──▶ bitmap ──(encode, cached by EncodeJob)──▶ frame

- **Stage 1 (rasterize)** turns the source into a bitmap. A plain bitmap (``StaticSource``) resolves
  inline — no worker, no placeholder. Any other source runs ``render`` on a worker, so an expensive or
  multi-step rasterize (e.g. compile LaTeX -> PDF -> rasterize -> recolor) stays off the event loop.
- **Stage 2 (encode)** is the heavy sixel encode. The ``EncodeJob`` is **crop-independent**, so a real
  paint and a ``get_style_at`` probe map to the same job — the cache never thrashes between them.

Each stage paints a "rendering…" placeholder on a miss and ``refresh()``es when its worker resolves, so
the next paint is a cache hit. The event loop never blocks on either stage. ``prefetch`` warms the
pipeline for a neighbour.

Overlap & clipping policy: graphics pixels can't be partially drawn or z-composited, so the widget
*suppresses* its blob (paints background) whenever the paint wouldn't be a clean full rectangle:

- under another screen (``is_active``),
- under a same-screen float painted in front (``first_occluder``) — toast, tooltip, dropdown,
- partially clipped by an ancestor scroll (``visible_region != region``) — there is no window-crop here;
  an ``Image`` inside a scroll feed blanks at the edges and snaps back when fully on screen.

Subclasses add hover/selection overlays by overriding ``_overlays_for`` (see ``ImageWithOverlays``).
"""

from collections import OrderedDict
from functools import partial

from rich.segment import Segment
from rich.style import Style
from textual.dom import NoScreen
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget
from textual.worker import Worker, WorkerState

from rhizome.tui.graphics.environment import active_backend, cell_metrics, unavailable_reason
from rhizome.tui.graphics.render.backend import GraphicsBackend
from rhizome.tui.graphics.render.geometry import placement
from rhizome.tui.graphics.render.source import RenderContext, StaticSource, as_source

_FRAMES_MAX = 8                         # encoded frames kept (current + neighbours)
_BITMAPS_MAX = 8                        # rasterized bitmaps kept (current + neighbours)
_FRAME_GROUP = "graphics-frame"
_RASTERIZE_GROUP = "graphics-rasterize"


def first_occluder(widget: Widget) -> Widget | None:
    """The first same-screen widget painted in front of ``widget`` that overlaps it, or None.

    Sixel pixels have no z-order: a widget floating over the image either gets buried under the blob or
    knocks the blob's escapes out of the compositor's cuts — which one is a race on geometry. The widget
    suppresses its blob while anything overlaps, so floats degrade exactly like modal screens: image
    hides, repaints from cache when the float leaves.

    Walks the screen compositor's map: in front = higher paint order; the widget's own descendants (its
    scrollbars) are exempt. Leans on private compositor state — fails open (None) if that structure moves
    under a Textual upgrade.
    """
    try:
        compositor = widget.screen._compositor
        cmap = compositor._visible_map if compositor._visible_map is not None else compositor._full_map
        mine = cmap[widget]
    except (NoScreen, KeyError, AttributeError):
        return None
    area = mine.visible_region
    for other, geometry in cmap.items():
        if geometry.order <= mine.order or widget in other.ancestors_with_self:
            continue
        if area.overlaps(geometry.visible_region):
            return other
    return None


class Throttle:
    """Mixin: coalesce expensive repaints to at most one per interval, latest-wins.

    A sixel redraw is slower than the mouse crosses regions, so hover/selection feedback throttles its
    repaints: the leading edge paints immediately, further requests during the cooldown only mark dirty,
    and the trailing edge paints once more at whatever state the cursor reached. Hosts (``ImageWithOverlays``
    for hover, the selection widgets for drag) call ``_request_repaint``.
    """

    _REPAINT_INTERVAL = 0.06            # min seconds between throttled repaints

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._repaint_timer = None
        self._repaint_dirty = False

    def _request_repaint(self) -> None:
        if self._repaint_timer is not None:
            self._repaint_dirty = True
            return
        self.refresh()                                         # leading edge: paint the live state now
        self._repaint_dirty = False
        self._repaint_timer = self.set_timer(self._REPAINT_INTERVAL, self._on_repaint_cooldown)

    def _on_repaint_cooldown(self) -> None:
        self._repaint_timer = None
        if self._repaint_dirty:                                # state changed during cooldown -> paint latest
            self._request_repaint()


class Image(Widget):
    """Draw a bitmap (or a source's bitmap) in the terminal, encoded on a worker thread.

    ``fit`` decides how the bitmap is scaled into the widget's cell box. ``"contain"`` (default) fills the
    box, up- or down-scaling to fit; ``"native"`` draws the bitmap at its own pixels, centered, shrinking
    only to avoid overflow. They differ *only* when the box is larger than the bitmap (when ``"contain"``
    would upscale). For a ``1fr`` widget the box just tracks the pane, so a font-zoom leaves the image's
    physical size unchanged either way (the pane is fixed; cells get bigger but fewer fit). The two diverge
    on a *fixed* cell footprint (e.g. ``width: 60; height: 30``), where the box is ``cells × cell-px`` and a
    font-zoom flips which side is larger::

        font-zoom IN  (cells grow, box outgrows the bitmap):
            "contain" → keeps filling 60×30 cells: image grows with the font (soft past 1:1)
            "native"  → stays 1:1: image holds its physical size, occupies fewer cells, slack grows
        font-zoom OUT (cells shrink, box smaller than the bitmap):
            "contain" == "native": both shrink to fit the box

    So a fixed-footprint ``"contain"`` image grows with the font; ``"native"`` holds its size and never
    upscale-blurs. Math-style content pairs ``"native"`` with a ``ctx.cell``-sized source: the source
    re-renders larger as cells grow, so the image tracks the text size and stays crisp.
    """

    DEFAULT_CSS = """
    Image { width: 1fr; height: 1fr; }
    """

    def __init__(self, backend: GraphicsBackend | None = None, *, halign: str = "center",
                 fit: str = "contain",
                 name: str | None = None, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._backend_override = backend                      # None -> read the global environment
        self._halign = halign
        self._fit = fit                                       # "contain" fills the box | "native" never enlarges
        self._source = None                                   # the ImageSource (set by show)
        self._bitmap = None                                   # last resolved bitmap (for hit-testing); None until painted
        self._bitmaps: "OrderedDict[object, Worker]" = OrderedDict()      # cache_key -> rasterize worker
        self._frames: "OrderedDict[object, Worker]" = OrderedDict()       # job -> page-encode worker
        self._occluded = False                                # a same-screen float overlaps us right now

    # -- public API ------------------------------------------------------------------------------

    @property
    def backend(self) -> GraphicsBackend | None:
        """The override if one was passed, else the process-global selected backend (or None)."""
        return self._backend_override if self._backend_override is not None else active_backend()

    def show(self, source) -> None:
        """Display ``source`` (a bitmap or an ``ImageSource``). Work happens at the next paint."""
        self._source = as_source(source)
        self._bitmap = None
        self.refresh()

    def prefetch(self, source) -> None:
        """Warm the caches for ``source`` (bitmap or ImageSource) at the current layout — neighbour prefetch.

        A ``StaticSource`` warms straight to the encoded frame; any other source warms the rasterize stage
        (its frame warms once the bitmap lands).
        """
        source = as_source(source)
        ctx = self._render_context()
        if ctx is None:
            return
        if isinstance(source, StaticSource):
            self._ensure_frame(self._job_for(source.render(ctx), ctx))
        else:
            self._ensure_bitmap(source, source.cache_key(ctx), ctx)

    # -- rendering -------------------------------------------------------------------------------

    def render_lines(self, crop: Region) -> list[Strip]:
        try:
            if self.backend is None:
                reason = unavailable_reason()
                return self._notice(crop, reason.value if reason else "terminal graphics unavailable")
            if self._source is None or not self.screen.is_active:
                return self._notice(crop)
        except NoScreen:
            return self._notice(crop)

        occluded = first_occluder(self) is not None
        if occluded != self._occluded:                        # a float arrived or left: repaint the whole
            self._occluded = occluded                         # image area, not just this paint's crop
            self.refresh()
        if occluded:
            return self._notice(crop)

        region = self._visible_region()
        if region is None or region != self.region:           # off-screen or clipped by an ancestor scroll
            return self._notice(crop)                         # a sixel can't be partially drawn — suppress

        ctx = self._render_context()
        if ctx is None:
            return self._notice(crop)

        bitmap = self._resolve_bitmap(ctx)                    # stage 1: rasterize (off-thread unless static)
        if bitmap is None:
            return self._notice(crop, "rendering…")
        self._bitmap = bitmap                                 # remembered for subclasses' hit-testing

        frame_worker = self._ensure_frame(self._job_for(bitmap, ctx))   # stage 2: encode (off-thread)
        if frame_worker.state is not WorkerState.SUCCESS:
            return self._notice(crop, "rendering…")
        frame = frame_worker.result
        return self.backend.compose(frame, self._overlays_for(frame.job, frame), crop, region=region)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS:
            self.refresh()                                    # a bitmap/frame landed -> repaint hits the cache
        elif event.state is WorkerState.ERROR:
            self.log.warning(f"graphics worker failed: {event.worker.error!r}")

    # -- overlay hook (overridden by ImageWithOverlays) ------------------------------------------

    def _overlays_for(self, job, frame) -> list:
        """The overlays to composite over the page this paint. Base draws none; subclasses override."""
        return []

    def _forget(self, job) -> None:
        """Drop anything a subclass tied to an evicted frame (overlays, etc.). Base keeps nothing."""

    # -- internals -------------------------------------------------------------------------------

    def _resolve_bitmap(self, ctx: RenderContext):
        """Stage 1 — the source's bitmap, or None while a worker is still producing it."""
        if isinstance(self._source, StaticSource):
            return self._source.render(ctx)                   # inline: no worker, no placeholder flash
        worker = self._ensure_bitmap(self._source, self._source.cache_key(ctx), ctx)
        return worker.result if worker.state is WorkerState.SUCCESS else None

    def _ensure_bitmap(self, source, key, ctx) -> Worker:
        """Get-or-dispatch the rasterize worker for ``key`` (idempotent — in-flight keys are reused)."""
        worker = self._bitmaps.get(key)
        if worker is None or worker.state in (WorkerState.CANCELLED, WorkerState.ERROR):
            worker = self.run_worker(partial(source.render, ctx), thread=True,
                                     group=_RASTERIZE_GROUP, description="rasterize image source",
                                     exit_on_error=False)
            self._bitmaps[key] = worker
            while len(self._bitmaps) > _BITMAPS_MAX:
                self._bitmaps.popitem(last=False)[1].cancel()
        self._bitmaps.move_to_end(key)
        return worker

    def _ensure_frame(self, job) -> Worker:
        """Get-or-dispatch the encode worker for ``job`` (idempotent — in-flight jobs are reused)."""
        worker = self._frames.get(job)
        if worker is None or worker.state in (WorkerState.CANCELLED, WorkerState.ERROR):
            worker = self.run_worker(partial(self.backend.encode, job), thread=True,
                                     group=_FRAME_GROUP, description="encode graphics frame",
                                     exit_on_error=False)
            self._frames[job] = worker
            while len(self._frames) > _FRAMES_MAX:
                evicted_job, evicted = self._frames.popitem(last=False)
                evicted.cancel()
                self._forget(evicted_job)
        self._frames.move_to_end(job)
        return worker

    def _render_context(self) -> RenderContext | None:
        cell = cell_metrics().current
        cw, ch = self.content_size.width, self.content_size.height
        if cw <= 0 or ch <= 0:
            return None
        return RenderContext(cell, cw, ch, self._background_rgba())

    def _max_scale(self) -> float:
        """The image-px -> box-px scale cap for this widget's ``fit`` — shared by draw and hit-test."""
        return 1.0 if self._fit == "native" else float("inf")

    def _job_for(self, bitmap, ctx: RenderContext):
        """An ``EncodeJob`` for ``bitmap`` at the layout described by ``ctx``."""
        box_w, box_h = ctx.content_width * ctx.cell.width, ctx.content_height * ctx.cell.height
        place = placement(bitmap.width, bitmap.height, box_w, box_h, self._halign, self._max_scale())
        return self.backend.prepare(bitmap, place, ctx.cell, background=ctx.background)

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
