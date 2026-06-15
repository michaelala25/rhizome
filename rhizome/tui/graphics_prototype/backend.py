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

import sys
from abc import ABC, abstractmethod

from textual.geometry import Region
from textual.strip import Strip
from textual_image._terminal import CellSize, TerminalError, capture_terminal_response, get_cell_size

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

    Call once at startup, before the Textual app starts — ``available`` queries the terminal, and this
    also resolves px-per-cell geometry while stdin is still ours (``_seed_cell_size``). Sixel is
    preferred over kitty/TGP only because it's the implemented one today; the order is not a quality
    judgement.
    """
    from rhizome.tui.graphics_prototype.kitty import KittyBackend
    from rhizome.tui.graphics_prototype.sixel import SixelBackend

    _seed_cell_size()

    for backend_cls in (SixelBackend, KittyBackend):
        if backend_cls.available():
            return backend_cls()
    return None


def _xtwinops(code: int) -> tuple[int, int] | None:
    """One XTWINOPS round-trip: ``CSI <code> t`` -> ``(height, width)``, or None on timeout/garbage."""
    try:
        with capture_terminal_response("\x1b[", "t", 0.5) as response:
            sys.__stdout__.write(f"\x1b[{code}t")
            sys.__stdout__.flush()
        _, height, width = (int(v) for v in response.sequence[2:-1].split(";"))
        return height, width
    except (TerminalError, TimeoutError, ValueError):
        return None


def _seed_cell_size() -> None:
    """Pin ``textual_image``'s cached cell size to the terminal's own XTWINOPS answer, when it gives one.

    ``get_cell_size`` trusts any non-zero TIOCGWINSZ pixel fields, but over ssh those are whatever the
    client stuffed into the pty — Windows OpenSSH sends a hardcoded 640x480, which implies a nonsense
    ~2x6 px cell and silently breaks image sizing and every cell<->px transform downstream. The escape
    reply is authoritative (it comes from the same program that will place the sixels), so it wins; no
    reply leaves the normal cascade alone. Must run before Textual's stdin reader would eat the reply.
    """
    if not (sys.__stdout__ and sys.__stdin__ and sys.__stdout__.isatty() and sys.__stdin__.isatty()):
        return

    cell = _xtwinops(16)                                    # direct: cell size in px
    if cell is None:
        area, chars = _xtwinops(14), _xtwinops(18)          # derive: text-area px / text-area cells
        if area and chars and chars[0] and chars[1]:
            cell = (area[0] // chars[0], area[1] // chars[1])

    if cell and cell[0] > 0 and cell[1] > 0:
        setattr(get_cell_size, "_result", CellSize(width=cell[1], height=cell[0]))
