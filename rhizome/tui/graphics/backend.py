"""The graphics-backend contract — "draw a bitmap (+ hover overlays) in *this* terminal."

A backend is **content-dumb**: it knows nothing of PDFs, pages, or resources. It is handed a bitmap
and box rectangles (in image px) and promises to render them through one terminal graphics protocol.
What differs per protocol — how a frame is encoded, and how a highlight composites over it — lives
behind these four methods and three opaque types; everything above (geometry, hit-testing, the
worker/cache machinery, the widget) is protocol-neutral.

Threading is *not* the backend's concern. ``prepare`` runs on the main thread (it snapshots live
inputs into an immutable, hashable ``EncodeJob``); ``encode`` is the heavy, pure step a worker thread
runs off that snapshot; ``compose`` runs on the main thread during paint. The caller owns the worker
and the frame cache (keyed by the ``EncodeJob`` itself), so the backend stays a set of pure functions.

Backends do **not** fall back to half-cell/unicode: ``select_backend`` returns None when the terminal
can't do true graphics, and the caller rejects rather than degrade.
"""

from abc import ABC, abstractmethod

from textual.geometry import Region
from textual.strip import Strip

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

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Whether this protocol is usable in the current terminal.

        Queries the terminal, so it must run at startup, before the Textual app takes over stdin.
        """

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
    def encode_highlight(self, frame: EncodedFrame, rect) -> Highlight | None:
        """Worker or main thread: encode the overlay for one box (image-px ``rect``), or None if empty."""

    @abstractmethod
    def compose(self, frame: EncodedFrame, highlight: Highlight | None, crop: Region, *,
                region: Region) -> list[Strip]:
        """Main thread, from ``render_lines``: assemble the paint Strips, compositing the overlay.

        ``region`` is the widget's visible region in screen coordinates (graphics protocols place
        their output by absolute position, not relative to the strip).
        """


def select_backend() -> GraphicsBackend | None:
    """The best graphics backend the terminal supports, or None to reject (no half-cell fallback).

    Call once at startup, before the Textual app starts — ``available`` queries the terminal. Sixel is
    preferred over kitty/TGP only because it's the implemented one today; the order is not a quality
    judgement.
    """
    from rhizome.tui.graphics.kitty import KittyBackend
    from rhizome.tui.graphics.sixel import SixelBackend

    for backend_cls in (SixelBackend, KittyBackend):
        if backend_cls.available():
            return backend_cls()
    return None
