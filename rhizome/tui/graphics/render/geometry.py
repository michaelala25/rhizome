"""The letterbox transform shared by every graphics backend — pure math, no PIL, no Textual.

A bitmap is shown by fitting it into the widget's content box (a rectangle measured in terminal cells,
here already converted to pixels) keeping its aspect ratio, centered/aligned on a background canvas.
``Placement`` captures that fit once; the backend applies it to *draw* the page and a hover hit-test
``footprint``-s a mouse cell back through it to *find* the box under the cursor. Computing the transform
in exactly one place is what keeps the rendered page and the boxes hit-tested against it from drifting
apart.
"""

from typing import NamedTuple


class Placement(NamedTuple):
    """Where a bitmap lands inside a content box — the full forward transform, in pixels.

    ``scale`` maps image px -> box px; the scaled image of ``scaled_w`` x ``scaled_h`` sits at
    ``(off_x, off_y)`` on a ``box_w`` x ``box_h`` canvas (the letterbox slack fills the rest).
    """
    img_w: int
    img_h: int
    box_w: int
    box_h: int
    scale: float
    scaled_w: int
    scaled_h: int
    off_x: int
    off_y: int


def placement(img_w: int, img_h: int, box_w: int, box_h: int, halign: str = "center",
              max_scale: float = float("inf")) -> Placement:
    """Fit an ``img_w`` x ``img_h`` bitmap into a ``box_w`` x ``box_h`` box keeping aspect ratio.

    ``halign`` ("left" | "center" | "right") sets the horizontal placement; the page is always
    vertically centered. The box is clamped to at least 1x1 so degenerate sizes don't divide by zero.

    ``max_scale`` caps the image-px -> box-px ratio. The default (no cap) is "contain": fill the box,
    up- or down-scaling as needed. ``max_scale=1.0`` is "native": never enlarge past the bitmap's own
    pixels — draw it at intrinsic size, centered, shrinking only when it would overflow the box.
    """
    box_w, box_h = max(1, box_w), max(1, box_h)
    scale = min(box_w / img_w, box_h / img_h, max_scale)
    scaled_w, scaled_h = max(1, round(img_w * scale)), max(1, round(img_h * scale))
    off_x = {"left": 0, "right": box_w - scaled_w}.get(halign, (box_w - scaled_w) // 2)
    off_y = (box_h - scaled_h) // 2
    return Placement(img_w, img_h, box_w, box_h, scale, scaled_w, scaled_h, off_x, off_y)


def footprint(p: Placement, cell_x: int, cell_y: int, cell_w: int, cell_h: int) -> tuple[float, float, float, float]:
    """Image-px rectangle covered by one terminal cell — the inverse of ``placement``.

    Maps the cell's *whole footprint* (not just its center) back to image space so a sub-cell box is
    still hittable at one-cell mouse resolution. Cells over the letterbox slack map outside the image;
    that's fine — they simply overlap no box.
    """
    left = (cell_x * cell_w - p.off_x) / p.scale
    top = (cell_y * cell_h - p.off_y) / p.scale
    right = ((cell_x + 1) * cell_w - p.off_x) / p.scale
    bottom = ((cell_y + 1) * cell_h - p.off_y) / p.scale
    return left, top, right, bottom


def first_hit(box: tuple[float, float, float, float], rects: list[tuple[float, float, float, float]]) -> int | None:
    """Index of the first ``rect`` in ``rects`` overlapping ``box``, or None — first-wins disambiguation."""
    bl, bt, br, bb = box
    for index, (x0, y0, x1, y1) in enumerate(rects):
        if bl < x1 and x0 < br and bt < y1 and y0 < bb:
            return index
    return None
