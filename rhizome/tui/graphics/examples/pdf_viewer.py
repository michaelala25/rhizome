"""Minimal example: rasterize one PDF page and view it in the terminal via ``rhizome.tui.graphics``.

The smallest end-to-end use of the library, and the canonical shape every consumer follows:

  1. ``graphics.initialize()`` BEFORE the Textual app starts. It probes the terminal while stdin is still
     ours (see the package ``DOC.md`` §1.4); skip it and no backend is selected.
  2. Rasterize a page to a Pillow bitmap with PyMuPDF — this script is the "content source" the graphics
     layer stays ignorant of.
  3. Hand the bitmap to an ``Image`` (no backend argument — it reads the one ``initialize`` chose). The
     widget owns encoding, off-thread caching, and placement.
  4. Forward resize events to ``graphics.note_resize`` so the cell size stays live (e.g. on font zoom, on
     terminals that report pixel size via in-band resize).

A one-row footer keeps the page sixel off the screen's last line: re-emitting a sixel that touches the
bottom row scrolls the terminal (see ``Image``'s docstring).

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_viewer [PDF] [--page N]
Needs a sixel terminal. ``img2sixel`` is optional — a pure-Python encoder is used when it's absent.
"""

import argparse
import os
import sys

import pymupdf
from PIL import Image as PILImage
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Static

import rhizome.tui.graphics as graphics

RENDER_ZOOM = 2.0       # rasterize above native point size so glyphs stay crisp when scaled to fit


def rasterize_page(pdf_path: str, page_index: int, zoom: float = RENDER_ZOOM) -> PILImage.Image:
    """Render one PDF page to an RGB bitmap — the 'source' the graphics layer knows nothing about."""
    page = pymupdf.open(pdf_path)[page_index]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)


class PdfViewerApp(App):
    """View a single rasterized PDF page as a terminal bitmap."""

    BINDINGS = [("q", "quit", "Quit")]

    # #footer reserves the bottom row so the page sixel never reaches the screen's last line.
    CSS = """
    #page { width: 1fr; height: 1fr; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, bitmap: PILImage.Image) -> None:
        super().__init__()
        self._bitmap = bitmap

    def compose(self) -> ComposeResult:
        yield graphics.Image(id="page")                    # backend comes from the global environment
        yield Static("q to quit", id="footer")

    def on_mount(self) -> None:
        self.query_one("#page", graphics.Image).show(self._bitmap)

    def on_resize(self, event: events.Resize) -> None:
        # Keep the cell size live: the emulator reports window pixels here on terminals that support
        # in-band resize. A no-op when pixel_size is None (a pure window resize doesn't change cell px).
        graphics.note_resize(self.size.width, self.size.height, event.pixel_size)


def main() -> None:
    parser = argparse.ArgumentParser(description="View a rasterized PDF page via rhizome.tui.graphics.")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index (default: 0).")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    # MUST run before the Textual app starts — it probes the terminal while stdin is still ours.
    env = graphics.initialize()
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    PdfViewerApp(rasterize_page(args.pdf, args.page)).run()


if __name__ == "__main__":
    main()
