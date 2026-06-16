"""Example: hover the text blocks of a PDF page — outline the one under the cursor, echo its text.

Shows ``ImageWithOverlays``: the same off-thread page render as the basic viewer, plus ``(rect, payload)``
regions the widget hit-tests and outlines on hover. Rasterizing the page and extracting the text-block
rectangles is this script's job (the "content source"); the widget owns encoding, the off-thread overlay
cache, hit-testing, and the ``RegionHovered`` / ``RegionSelected`` messages.

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_text [PDF] [--page N]
Needs a sixel terminal (img2sixel optional). ``q`` quits.
"""

import argparse
import os
import sys

import pymupdf
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

import rhizome.tui.graphics as graphics
from rhizome.tui.graphics.examples.pdf_viewer import RENDER_ZOOM, rasterize_page


def extract_text_blocks(pdf_path: str, page_index: int) -> list[tuple[tuple, str]]:
    """Text blocks as ``(rect_in_image_px, text)`` — image-px rect == PDF point × RENDER_ZOOM."""
    page = pymupdf.open(pdf_path)[page_index]
    regions = []
    for x0, y0, x1, y1, text, _block_no, block_type in page.get_text("blocks"):
        if block_type == 0 and text.strip():
            rect = (x0 * RENDER_ZOOM, y0 * RENDER_ZOOM, x1 * RENDER_ZOOM, y1 * RENDER_ZOOM)
            regions.append((rect, text.strip()))
    return regions


class PdfTextApp(App):
    """Single page; hovering a text block outlines it and echoes its text into the side panel."""

    BINDINGS = [("q", "quit", "Quit")]

    # #footer reserves the bottom row so the page sixel never reaches the screen's last line.
    CSS = """
    #row { height: 1fr; }
    #page { width: 2fr; height: 1fr; }
    #info { width: 1fr; height: 1fr; border: round $accent; padding: 1 2; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, bitmap, regions) -> None:
        super().__init__()
        self._bitmap = bitmap
        self._regions = regions

    def compose(self) -> ComposeResult:
        yield Horizontal(graphics.ImageWithOverlays(id="page"), Static(id="info"), id="row")
        yield Static("hover a block to outline it · q to quit", id="footer")

    def on_mount(self) -> None:
        self._echo(None)
        self.query_one("#page", graphics.ImageWithOverlays).show(self._bitmap, self._regions)

    def on_image_with_overlays_region_hovered(self, message: graphics.ImageWithOverlays.RegionHovered) -> None:
        self._echo(message.index, message.payload or "")

    def _echo(self, index: int | None, text: str = "") -> None:
        info = self.query_one("#info", Static)
        info.update("[dim]— hover a text block —[/dim]" if index is None
                    else f"[b]block {index}[/b]\n\n{escape(text)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hover a PDF text block; outline it via rhizome.tui.graphics.")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index (default: 0).")
    args = parser.parse_args()
    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    env = graphics.initialize()                              # before the app starts — probes the terminal
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    PdfTextApp(rasterize_page(args.pdf, args.page), extract_text_blocks(args.pdf, args.page)).run()


if __name__ == "__main__":
    main()
