"""Example: the full roundtrip — scroll, zoom, AND drag-select text on a PDF page.

``ScrollSelectableImage`` marries the pan/zoom of ``pdf_scroll`` with the word selection of ``pdf_select``.
The page rasterizes once (well above native size, so zoomed-in pixels stay crisp); the footprint pins at
first layout and re-encodes a viewport-window per scroll offset; and dragging selects a word range whose
per-line tint bars are encoded in canvas-absolute cells (clipped to the window), so a selection survives
both zoom and scroll. The side panel echoes the selected text.

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_scroll_select [PDF] [--page N]
Keys: arrows/PageUp/PageDown/wheel pan · +/- zoom · 0 reset · drag selects · esc clears · q quits.
Needs a sixel terminal (img2sixel optional).
"""

import argparse
import os
import sys

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

import rhizome.tui.graphics as graphics
from rhizome.tui.graphics.render.scroll import ZOOM_STEP
from rhizome.tui.graphics.examples.pdf_select import extract_words
from rhizome.tui.graphics.examples.pdf_viewer import rasterize_page

RASTER_ZOOM = 3.0       # rasterize well above native size so zoomed-in pixels stay crisp


class PdfScrollSelectApp(App):
    """Pan/zoom the page and drag across words; the side panel echoes the selected text."""

    BINDINGS = [("q", "quit", "Quit"), ("escape", "clear", "Clear")]

    # #footer reserves the bottom row so the page sixel never reaches the screen's last line.
    CSS = """
    #row { height: 1fr; }
    #page { width: 3fr; height: 1fr; }
    #info { width: 1fr; height: 1fr; border: round $accent; padding: 1 2; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, bitmap, words) -> None:
        super().__init__()
        self._bitmap = bitmap
        self._words = words

    def compose(self) -> ComposeResult:
        yield Horizontal(graphics.ScrollSelectableImage(id="page"), Static(id="info"), id="row")
        yield Static("drag selects · +/- zoom · 0 reset · arrows/wheel pan · esc clears · q quits", id="footer")

    def on_mount(self) -> None:
        self._echo("", 0)
        page = self.query_one("#page", graphics.ScrollSelectableImage)
        page.show(self._bitmap, self._words)
        page.focus()                                    # so arrow keys / wheel scroll the page

    def on_key(self, event: events.Key) -> None:
        page = self.query_one("#page", graphics.ScrollSelectableImage)
        if event.character in ("+", "="):
            page.set_zoom(page.zoom * ZOOM_STEP)
        elif event.character == "-":
            page.set_zoom(page.zoom / ZOOM_STEP)
        elif event.character == "0":
            page.set_zoom(1.0)

    def on_scroll_selectable_image_selection_changed(
            self, message: graphics.ScrollSelectableImage.SelectionChanged) -> None:
        self._echo(message.text, message.count)

    def _echo(self, text: str, count: int) -> None:
        info = self.query_one("#info", Static)
        info.update("[dim]— drag across words to select —[/dim]" if not count
                    else f"[b]{count} words[/b]\n\n{escape(text)}")

    def action_clear(self) -> None:
        self.query_one("#page", graphics.ScrollSelectableImage).clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scroll, zoom, and drag-select PDF text via rhizome.tui.graphics.")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index (default: 0).")
    args = parser.parse_args()
    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    env = graphics.initialize()                          # before the app starts — probes the terminal
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    bitmap = rasterize_page(args.pdf, args.page, zoom=RASTER_ZOOM)
    words = extract_words(args.pdf, args.page, zoom=RASTER_ZOOM)   # word rects must match the raster zoom
    PdfScrollSelectApp(bitmap, words).run()


if __name__ == "__main__":
    main()
