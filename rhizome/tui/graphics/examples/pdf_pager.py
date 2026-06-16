"""Example: page through a PDF, rasterizing each page off the event loop via an ``ImageSource``.

Shows the source pipeline. Instead of a ready bitmap, the widget is handed a ``PdfPageSource`` that
rasterizes its page on a worker thread — so page turns don't block the UI (a brief "rendering…" on first
visit, instant on revisit, since both the rasterize and the encode are cached). Neighbours are
``prefetch``-ed, so the common next/prev already has its bitmap warm.

This is the contrast with ``pdf_viewer`` / ``pdf_text``, which rasterize on the main thread and hand over
a finished bitmap: here the rasterization itself is the off-thread work the ``Image`` drives.

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_pager [PDF]
Keys: →/PageDown/space next · ←/PageUp prev · q quits.   Needs a sixel terminal (img2sixel optional).
"""

import argparse
import os
import sys

import pymupdf
from PIL import Image as PILImage
from textual.app import App, ComposeResult
from textual.widgets import Static

import rhizome.tui.graphics as graphics
from rhizome.tui.graphics import RenderContext

RENDER_ZOOM = 2.0       # rasterize above native point size so glyphs stay crisp when scaled to fit


class PdfPageSource:
    """Rasterize one PDF page to a bitmap on a worker thread.

    ``cache_key`` ignores the layout (fixed render zoom), so a page is rasterized once and reused across
    resizes and revisits. Each ``render`` opens its own PyMuPDF handle — no shared-document race between
    worker threads.
    """

    def __init__(self, path: str, page: int) -> None:
        self.path = path
        self.page = page

    def cache_key(self, ctx: RenderContext) -> object:
        return (self.path, self.page, RENDER_ZOOM)

    def render(self, ctx: RenderContext) -> PILImage.Image:
        page = pymupdf.open(self.path)[self.page]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM), alpha=False)
        return PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)


class PdfPagerApp(App):
    """Page through a PDF; each page rasterizes off-thread and neighbours are prefetched."""

    BINDINGS = [
        ("right,pagedown,space", "turn(1)", "Next"),
        ("left,pageup", "turn(-1)", "Prev"),
        ("q", "quit", "Quit"),
    ]

    # #footer reserves the bottom row so the page sixel never reaches the screen's last line.
    CSS = """
    #page { width: 1fr; height: 1fr; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, path: str, num_pages: int) -> None:
        super().__init__()
        self._path = path
        self._num_pages = num_pages
        self._page = 0

    def compose(self) -> ComposeResult:
        yield graphics.Image(id="page")
        yield Static(id="footer")

    def on_mount(self) -> None:
        self._render_page()

    def action_turn(self, delta: int) -> None:
        page = max(0, min(self._num_pages - 1, self._page + delta))
        if page != self._page:
            self._page = page
            self._render_page()

    def _render_page(self) -> None:
        self.query_one("#page", graphics.Image).show(PdfPageSource(self._path, self._page))
        self.call_after_refresh(self._prefetch_neighbours)     # prefetch needs a laid-out size (a RenderContext)
        self.query_one("#footer", Static).update(
            f"page {self._page + 1}/{self._num_pages}  ·  ←/→ to turn · q to quit")

    def _prefetch_neighbours(self) -> None:
        image = self.query_one("#page", graphics.Image)
        for neighbour in (self._page - 1, self._page + 1):     # warm the common next/prev rasterize
            if 0 <= neighbour < self._num_pages:
                image.prefetch(PdfPageSource(self._path, neighbour))


def main() -> None:
    parser = argparse.ArgumentParser(description="Page through a PDF; pages rasterize off-thread.")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    args = parser.parse_args()
    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    env = graphics.initialize()                              # before the app starts — probes the terminal
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    PdfPagerApp(args.pdf, pymupdf.open(args.pdf).page_count).run()


if __name__ == "__main__":
    main()
