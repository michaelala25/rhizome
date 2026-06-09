"""Sixel graphics backend: a bitmap (+ hover overlays) drawn via libsixel's ``img2sixel``.

The full page is letterboxed onto a background canvas and encoded once into a sixel DCS string. A
hover overlay for a box is its *cell-aligned* region cropped from that same canvas with a border
drawn on it, encoded once too — opaque, so ``compose`` just blits it after the page sixel in the same
strip and the terminal draws it on top (no transparency, no z-index). The page sixel is never
re-encoded for a hover.

The ``EncodeJob`` deliberately excludes the paint ``crop``: a fully visible image always paints its
whole region, so one job per (bitmap, placement) covers every paint and every ``get_style_at`` probe
alike — the cache can't thrash between them. A partially-scrolled sixel is imperfect (the blob can't
cell-scroll); that's an accepted protocol limitation, not handled here.

Requires the ``img2sixel`` binary (Debian/Ubuntu: ``apt install libsixel-bin``) and a sixel terminal.
"""

import io
import math
import shutil
import subprocess
from typing import NamedTuple

from PIL import Image as PILImage
from PIL import ImageDraw
from rich.color import Color
from rich.control import Control
from rich.segment import ControlType, Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip

from rhizome.tui.graphics.backend import GraphicsBackend
from rhizome.tui.graphics.geometry import Placement

_NULL = Style()                     # control-only segments carry no style
OUTLINE_RGB = (220, 30, 30)         # hover outline color
OUTLINE_WIDTH = 3                   # canvas px — drawn in the scaled (on-screen) space


class SixelOptions(NamedTuple):
    """Encode knobs traded against blob size / redraw speed.

    ``colors`` is the img2sixel palette (only <=16 meaningfully shrinks the blob, and that mangles
    anti-aliased text). ``scale`` downsamples before encoding — shrinks the blob ~quadratically but
    also shrinks the image on screen, since sixels draw at native px.
    """
    colors: int = 256
    scale: float = 1.0


class SixelEncodeJob:
    """Immutable, hashable snapshot of a full-frame encode — also the frame-cache key.

    Equality/hash key on the *identity* of the bitmap (not a pixel compare): the cache holds the job,
    which holds the bitmap, so the id stays valid while the entry lives. Re-rasterizing a page yields
    a new object -> a new job -> a fresh encode, which is exactly right.
    """

    __slots__ = ("bitmap", "placement", "cell", "background", "options", "_key")

    def __init__(self, bitmap: PILImage.Image, placement: Placement, cell, background: tuple,
                 options: SixelOptions) -> None:
        self.bitmap = bitmap
        self.placement = placement
        self.cell = cell
        self.background = background
        self.options = options
        self._key = (id(bitmap), placement, cell, background, options)

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SixelEncodeJob) and self._key == other._key


class SixelFrame(NamedTuple):
    """A rendered page. ``scaled`` (the letterboxed canvas) is kept so overlays crop from it."""
    job: SixelEncodeJob
    scaled: PILImage.Image
    sixel: str


class SixelHighlight(NamedTuple):
    """An overlay's cell-aligned position (widget-relative) and its encoded sixel."""
    cell_x: int
    cell_y: int
    sixel: str


def render_letterboxed(image: PILImage.Image, p: Placement, background: tuple) -> PILImage.Image:
    """Apply a ``Placement``: scale ``image`` and paste it onto a box-sized ``background`` canvas.

    Always box-sized (and so image-shape-independent), which is what keeps a frame-cache key valid as
    bitmaps of any shape pass through the same widget.
    """
    canvas = PILImage.new("RGB", (p.box_w, p.box_h), background[:3])
    canvas.paste(image.resize((p.scaled_w, p.scaled_h)), (p.off_x, p.off_y))
    return canvas


def _encode_sixel(image: PILImage.Image, options: SixelOptions, background: tuple) -> str:
    """Pillow image -> sixel DCS via ``img2sixel``. Pure + GIL-releasing, so it runs on a worker."""
    if options.scale != 1.0:
        w, h = image.size
        image = image.resize((max(1, round(w * options.scale)), max(1, round(h * options.scale))))
    ppm = io.BytesIO()
    image.convert("RGB").save(ppm, format="PPM")       # uncompressed: cheap to pipe to the encoder
    cmd = ["img2sixel", "-d", "none", "-E", "size", "-p", str(options.colors)]  # no dither keeps text crisp
    return subprocess.run(cmd, input=ppm.getvalue(), stdout=subprocess.PIPE, check=True).stdout.decode("latin-1")


def _placement_segments(sixel: str, x: int, y: int, region: Region) -> list[Segment]:
    """Position a sixel blob at absolute cell ``(x, y)``, then park the cursor at the region's corner.

    The trailing ``CURSOR_FORWARD, 0`` keeps Textual from advancing the cursor over the blob itself.
    """
    return [
        Segment(Control.move_to(x, y).segment.text, style=_NULL),
        Segment(sixel, style=_NULL, control=((ControlType.CURSOR_FORWARD, 0),)),
        Segment(Control.move_to(region.right, region.bottom).segment.text, style=_NULL),
    ]


class SixelBackend(GraphicsBackend):
    """Encode + compose for the sixel protocol. Configured once with its ``SixelOptions``."""

    def __init__(self, options: SixelOptions = SixelOptions()) -> None:
        self._options = options

    @classmethod
    def available(cls) -> bool:
        if shutil.which("img2sixel") is None:
            return False
        from textual_image.renderable import sixel as _sixel     # import-time terminal query — keep it lazy
        return _sixel.query_terminal_support()

    def prepare(self, bitmap: PILImage.Image, placement: Placement, cell, *, background: tuple) -> SixelEncodeJob:
        return SixelEncodeJob(bitmap, placement, cell, background, self._options)

    def encode(self, job: SixelEncodeJob) -> SixelFrame:
        scaled = render_letterboxed(job.bitmap, job.placement, job.background)
        return SixelFrame(job, scaled, _encode_sixel(scaled, job.options, job.background))

    def encode_highlight(self, frame: SixelFrame, rect: tuple) -> SixelHighlight | None:
        """Crop the box's cell-aligned region from the scaled page, draw the border, encode just that."""
        p, cell = frame.job.placement, frame.job.cell
        x0, y0, x1, y1 = rect                                   # box in image px
        bx0, by0 = p.off_x + x0 * p.scale, p.off_y + y0 * p.scale   # -> scaled (canvas) px
        bx1, by1 = p.off_x + x1 * p.scale, p.off_y + y1 * p.scale
        content_w, content_h = p.box_w // cell.width, p.box_h // cell.height
        cx0, cy0 = max(0, int(bx0 // cell.width)), max(0, int(by0 // cell.height))
        cx1 = min(content_w, math.ceil(bx1 / cell.width))
        cy1 = min(content_h, math.ceil(by1 / cell.height))
        if cx1 <= cx0 or cy1 <= cy0:
            return None

        px0, py0, px1, py1 = cx0 * cell.width, cy0 * cell.height, cx1 * cell.width, cy1 * cell.height
        crop = frame.scaled.crop((px0, py0, px1, py1)).copy()
        ImageDraw.Draw(crop).rectangle([round(bx0 - px0), round(by0 - py0), round(bx1 - px0), round(by1 - py0)],
                                       outline=OUTLINE_RGB, width=OUTLINE_WIDTH)
        return SixelHighlight(cx0, cy0, _encode_sixel(crop, frame.job.options, frame.job.background))

    def compose(self, frame: SixelFrame, highlight: SixelHighlight | None, crop: Region, *,
                region: Region) -> list[Strip]:
        r, g, b = frame.job.background[:3]
        clear = Segment(" " * crop.width, style=Style(bgcolor=Color.from_rgb(r, g, b)))
        last = [clear, *_placement_segments(frame.sixel, region.x, region.y, region)]
        if highlight is not None:                               # overlay after the page -> drawn on top
            last += _placement_segments(highlight.sixel, region.x + highlight.cell_x,
                                        region.y + highlight.cell_y, region)
        lines = [Strip([clear], cell_length=crop.width) for _ in range(crop.height - 1)]
        lines.append(Strip(last, cell_length=crop.width))
        return lines
