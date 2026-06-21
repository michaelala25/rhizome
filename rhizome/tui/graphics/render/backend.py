"""The graphics-backend contract — "draw a bitmap (+ hover overlays) in *this* terminal."

A backend is **content-dumb**: it knows nothing of PDFs, pages, or resources. It is handed a bitmap
and box rectangles (in image px) and renders them through one terminal graphics protocol. What differs
per protocol — how a frame is encoded, how a highlight composites over it — lives behind these four
methods and three opaque types; everything above (geometry, hit-testing, the worker/cache machinery,
the widget) is protocol-neutral.

Threading is **not** the backend's concern. ``prepare`` runs on the main thread (it snapshots live
inputs into an immutable, hashable ``EncodeJob``); ``encode`` is the heavy, pure step a worker thread
runs off that snapshot; ``compose`` runs on the main thread during paint. The widget owns the worker
and the frame cache (keyed by the ``EncodeJob`` itself), so the backend stays a set of pure functions.

Capability detection and backend selection are NOT here — they live in ``terminal.capabilities`` and
``environment``. A backend is a pure renderer; whether it's usable is decided before one is constructed.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from textual.geometry import Region
from textual.strip import Strip


@dataclass(frozen=True)
class Outline:
    """Overlay style: a border drawn around the box — the hover affordance."""
    color: tuple = (220, 30, 30)
    width: int = 3


@dataclass(frozen=True)
class Fill:
    """Overlay style: a translucent wash over the box's cell-aligned crop — the selection affordance.

    A uniform wash is what makes overlapping selection tiles compose for free: any cell two tiles share
    blends to identical pixels, so opaque blits land correctly in any order.
    """
    color: tuple = (40, 110, 220)
    alpha: float = 0.36


OverlayStyle = Outline | Fill

# Opaque, backend-specific tokens. Their concrete shapes live in each backend module; the rest of the
# system only ever holds them as opaque handles and never inspects their fields.
#
#   EncodeJob     — an immutable, hashable snapshot of everything ``encode`` needs. Doubles as the
#                   frame-cache key, so "same inputs" == "same job" == "cache hit".
#   EncodedFrame  — a rendered, ready-to-blit frame (plus whatever ``encode_highlight`` reuses).
#   Highlight     — an encoded overlay for one box, composited over the frame by ``compose``.
EncodeJob = object
EncodedFrame = object
Highlight = object


class GraphicsBackend(ABC):
    """One terminal graphics protocol's encode + compose, behind a protocol-neutral surface."""

    @abstractmethod
    def prepare(self, bitmap, placement, cell_size, *, background) -> EncodeJob:
        """Main thread: snapshot the inputs ``encode`` needs into an immutable, hashable job.

        ``placement`` is the shared letterbox transform (the same instance used for hit-testing);
        ``cell_size`` is px-per-cell; ``background`` is the widget's resolved RGBA fill.
        """

    @abstractmethod
    def encode(self, job: EncodeJob) -> EncodedFrame:
        """Worker thread: the heavy, pure render of a full frame off ``job``."""

    @abstractmethod
    def encode_highlight(self, frame: EncodedFrame, rect, style: OverlayStyle) -> Highlight | None:
        """Worker or main thread: encode the overlay for one box (image-px ``rect``) in ``style``, or None.

        ``style`` is ``Outline`` (a border, for hover) or ``Fill`` (a translucent wash, for selection);
        the cell-aligned crop is identical either way, only the paint differs.
        """

    @abstractmethod
    def compose(self, frame: EncodedFrame, overlays: list[Highlight], crop: Region, *,
                region: Region) -> list[Strip]:
        """Main thread, from ``render_lines``: assemble the paint Strips, compositing any overlays.

        ``overlays`` are blitted after the page in order (so later ones draw on top — graphics pixels
        have no z-order). ``region`` is the widget's visible region in screen coordinates (graphics
        protocols place output by absolute position, not relative to the strip).
        """
