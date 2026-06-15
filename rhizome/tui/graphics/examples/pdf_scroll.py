"""Example: pan and zoom a single PDF page with a pinned cell footprint via ``ScrollImage``.

Shows the scroll model. The page rasterizes once (well above native size, so zoomed-in pixels stay
crisp), then ``ScrollImage`` pins its footprint at first layout and re-encodes a viewport-window per
scroll offset off the event loop. ``+``/``-`` zoom past the viewport; scrollbars/wheel/arrows pan.

Contrast with ``pdf_viewer``, which re-fits the whole page into its box so a font-zoom changes pixel
density but never apparent size. This example opts into ``zoom_tracks_font=True``, so a *terminal*
font-zoom also grows the page (and you scroll around the larger image), with the ``+``/``-`` zoom composing
on top — which is why it forwards resize events to ``note_resize`` to keep the cell size live.

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_scroll [PDF] [--page N]
Keys: arrows/PageUp/PageDown/wheel pan · +/- zoom · 0 reset · q quits.   Needs a sixel terminal.
"""

import argparse
import os
import sys

from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Static

import rhizome.tui.graphics as graphics
from rhizome.tui.graphics.render.scroll import ZOOM_STEP
from rhizome.tui.graphics.examples.pdf_viewer import rasterize_page


class PdfScrollApp(App):
    """Pan a pinned-footprint page with the scroll keys/wheel; +/- rescale the footprint."""

    BINDINGS = [("q", "quit", "Quit")]

    CSS = """
    #page { width: 1fr; height: 1fr; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, bitmap) -> None:
        super().__init__()
        self._bitmap = bitmap

    def compose(self) -> ComposeResult:
        yield graphics.ScrollImage(id="page", zoom_tracks_font=True)
        yield Static("+/- zoom · 0 reset · arrows/wheel pan · q quits", id="footer")

    def on_mount(self) -> None:
        page = self.query_one("#page", graphics.ScrollImage)
        page.show(self._bitmap)
        page.focus()                                    # so arrow keys / wheel scroll the page

    def on_resize(self, event: events.Resize) -> None:
        # Keep the cell size live so a font-zoom is detected and grows the page (zoom_tracks_font).
        graphics.note_resize(self.size.width, self.size.height, event.pixel_size)

    def on_key(self, event: events.Key) -> None:
        page = self.query_one("#page", graphics.ScrollImage)
        if event.character in ("+", "="):
            page.set_zoom(page.zoom * ZOOM_STEP)
        elif event.character == "-":
            page.set_zoom(page.zoom / ZOOM_STEP)
        elif event.character == "0":
            page.set_zoom(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pan/zoom a PDF page with a pinned footprint (sixel).")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index (default: 0).")
    parser.add_argument("--zoom", type=float, default=3.0, help="Rasterization zoom (sharpness; default: 3).")
    args = parser.parse_args()
    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    env = graphics.initialize()                          # before the app starts — probes the terminal
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    PdfScrollApp(rasterize_page(args.pdf, args.page, zoom=args.zoom)).run()


if __name__ == "__main__":
    main()
