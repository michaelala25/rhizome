"""Image sources: where a bitmap comes from, possibly produced off the event loop.

An ``Image`` doesn't have to be handed a ready bitmap — it can be handed a *source* that produces one, so
the rasterization itself runs on a worker. This matters when producing the bitmap is expensive or
multi-step (e.g. compile LaTeX -> PDF -> rasterize -> recolor): every step then lives off the event loop,
behind the same "rendering…" placeholder as the sixel encode.

A source declares two things:

  - ``cache_key(ctx)`` — a hashable identity for "the bitmap this source produces in this context".
    Include only what the output actually depends on: a fixed-zoom PDF page ignores ``ctx`` (rasterize
    once, reuse across resizes); a source that sizes glyphs to the terminal font keys on ``ctx.cell``.
  - ``render(ctx)``    — produce the RGB bitmap. Runs on a worker thread, so keep it pure and let it
    release the GIL (PyMuPDF, Pillow, and subprocesses all do).

``Image.show(bitmap)`` is sugar: a plain bitmap is wrapped in a ``StaticSource`` whose key is its
identity and whose ``render`` returns it unchanged — so the common path does no extra work, runs no
worker, and never flashes a placeholder.
"""

from typing import NamedTuple, Protocol, runtime_checkable

from PIL import Image as PILImage

from rhizome.tui.graphics.terminal.cellsize import CellSize


class RenderContext(NamedTuple):
    """What a source may need to rasterize: the cell size (px), the content box (cells), the bg fill."""
    cell: CellSize
    content_width: int          # content box width, in cells
    content_height: int         # content box height, in cells
    background: tuple           # widget's resolved RGBA fill


@runtime_checkable
class ImageSource(Protocol):
    """Produces a bitmap for an ``Image``, with a cache identity. ``render`` runs on a worker thread."""

    def cache_key(self, ctx: RenderContext) -> object: ...

    def render(self, ctx: RenderContext) -> PILImage.Image: ...


class StaticSource:
    """A source wrapping an already-rendered bitmap — the degenerate, no-work case.

    ``Image`` recognizes it and resolves it inline (no worker, no placeholder), so handing the widget a
    plain bitmap is exactly as cheap as before sources existed.
    """

    __slots__ = ("bitmap",)

    def __init__(self, bitmap: PILImage.Image) -> None:
        self.bitmap = bitmap

    def cache_key(self, ctx: RenderContext) -> object:
        return id(self.bitmap)

    def render(self, ctx: RenderContext) -> PILImage.Image:
        return self.bitmap


def as_source(bitmap_or_source) -> ImageSource:
    """Coerce a bitmap or a source into a source — plain PIL images become a ``StaticSource``."""
    if isinstance(bitmap_or_source, PILImage.Image):
        return StaticSource(bitmap_or_source)
    return bitmap_or_source
