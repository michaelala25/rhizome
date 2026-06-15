"""Example: drag across a PDF page's words to select a range, washed with a uniform tint.

Shows ``SelectableImage`` — the selection cousin of ``pdf_text``'s hover. Same off-thread page render,
but dragging anchor → focus fills the words between them (merged into per-line bars) and posts
``SelectionChanged``; the side panel echoes the selected text. ``esc`` clears, ``q`` quits.

Run:  uv run python -m rhizome.tui.graphics.examples.pdf_select [PDF] [--page N]
Needs a sixel terminal (img2sixel optional).
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
from rhizome.tui.graphics import Word
from rhizome.tui.graphics.examples.pdf_viewer import RENDER_ZOOM, rasterize_page


def extract_words(pdf_path: str, page_index: int, zoom: float = RENDER_ZOOM) -> list[Word]:
    """Words as ``Word(rect_in_image_px, text, block, line)`` — rect == PDF point × ``zoom`` (match the raster)."""
    page = pymupdf.open(pdf_path)[page_index]
    words = []
    for x0, y0, x1, y1, text, block, line, _word_no in page.get_text("words"):
        if text.strip():
            rect = (x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom)
            words.append(Word(rect, text, block, line))
    return words


class PdfSelectApp(App):
    """Drag across words to grow a selection; the side panel echoes the selected text."""

    BINDINGS = [("q", "quit", "Quit"), ("escape", "clear", "Clear")]

    # #footer reserves the bottom row so the page sixel never reaches the screen's last line.
    CSS = """
    #row { height: 1fr; }
    #page { width: 2fr; height: 1fr; }
    #info { width: 1fr; height: 1fr; border: round $accent; padding: 1 2; }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, bitmap, words) -> None:
        super().__init__()
        self._bitmap = bitmap
        self._words = words

    def compose(self) -> ComposeResult:
        yield Horizontal(graphics.SelectableImage(id="page"), Static(id="info"), id="row")
        yield Static(f"drag across words to select · {len(self._words)} words · esc clears · q quits", id="footer")

    def on_mount(self) -> None:
        self._echo("", 0)
        self.query_one("#page", graphics.SelectableImage).show(self._bitmap, self._words)

    def on_selectable_image_selection_changed(self, message: graphics.SelectableImage.SelectionChanged) -> None:
        self._echo(message.text, message.count)

    def _echo(self, text: str, count: int) -> None:
        info = self.query_one("#info", Static)
        info.update("[dim]— drag across words to select —[/dim]" if not count
                    else f"[b]{count} words[/b]\n\n{escape(text)}")

    def action_clear(self) -> None:
        self.query_one("#page", graphics.SelectableImage).clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Drag-select PDF words and tint them via rhizome.tui.graphics.")
    parser.add_argument("pdf", nargs="?", default="example.pdf", help="Path to the PDF (default: example.pdf).")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index (default: 0).")
    args = parser.parse_args()
    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    env = graphics.initialize()                              # before the app starts — probes the terminal
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")

    PdfSelectApp(rasterize_page(args.pdf, args.page), extract_words(args.pdf, args.page)).run()


if __name__ == "__main__":
    main()
