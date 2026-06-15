r"""Example: stream agent markdown but render ``$$ … $$`` spans as sixel math blocks.

The chat-feed use case for the graphics layer. An agent reply streams in a few characters at a time; each
*closed* ``$$ … $$`` display block pops into a sized, theme-colored image while the surrounding prose stays
plain ``Markdown``. It leans on three things the library is meant to make cheap:

- **Off-thread, multi-step rasterization (the ``ImageSource`` story).** Turning a block into pixels is slow
  and blocking — ``pdflatex`` -> ``pymupdf`` -> recolor. Wrapping that chain in a ``MathSource`` hands the
  whole thing to ``Image``'s worker pipeline: the block shows a "rendering…" placeholder and pops in when
  the bitmap (then its sixel) lands, and the event loop never stalls.
- **Clipping at a scroll edge, for free.** A sixel can't be partially drawn, and a chat feed scrolls. An
  ``Image`` already suppresses its blob whenever the paint wouldn't be a clean full rectangle — off screen,
  occluded by a float, or clipped by an ancestor scroll — so a block blanks at the viewport edges and snaps
  back when fully visible, with nothing to wire here.
- **Font-relative sizing via ``fit="native"``.** ``MathSource`` sizes each equation to a multiple of the
  *live* cell height, so the math tracks the terminal text size and re-rasterizes larger on a font-zoom
  (``note_resize`` feeds the new cell size). ``fit="native"`` then draws it at that intrinsic size instead
  of stretching it to fill the message column.

The markdown-splitting half (finding closed ``$$ … $$`` spans in an append-only stream) is content logic
the graphics layer knows nothing about — it lives here unchanged from any other ``$$`` renderer.

Run:  uv run python -m rhizome.tui.graphics.examples.chat_math
Needs a sixel terminal and a LaTeX install (``pdflatex``); ``img2sixel`` is optional (a pure-Python encoder
is used when it's absent). It auto-streams one message; then scroll up/down to test the clip behavior · q quits.
"""

import asyncio
import math
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from textual.worker import Worker, WorkerState

import rhizome.tui.graphics as graphics

# Rendered-math sizing + sharpness. The on-screen text target is MATH_TEXT_RATIO × the cell height; we
# rasterize at MATH_SUPERSAMPLE × that and downsample, so the encoded image is always supersampled relative
# to its on-screen size (crisp edges). Deriving the size from the *live* cell height (carried on the source's
# RenderContext) is what makes a block re-rasterize sharp — and bigger — when the terminal font zooms.
MATH_PT = 12             # LaTeX nominal font pt baked into the standalone doc class
MATH_TEXT_RATIO = 1.20   # rendered math height ÷ terminal text height (1.0 = parity; <1 tighter; >1 bigger)
MATH_SUPERSAMPLE = 2     # rasterize this many × display res, then LANCZOS-downsample -> crisp anti-aliased edges


# ========================================================================================================
# RASTERIZE HALF — LaTeX body -> tight, theme-colored bitmap (the "content source", library-agnostic)
# ========================================================================================================

# border={<horizontal> <vertical>}: generous left/right + a little top/bottom so glyphs never reach the
# bitmap's edge cells (the last cell can get clipped when the blob is blitted tight to its box).
STANDALONE = r"""\documentclass[border={12pt 6pt},12pt]{standalone}
\usepackage{amsmath}
\usepackage{amssymb}
\begin{document}
%s
\end{document}
"""


class MathRenderError(Exception):
    """``pdflatex`` produced no PDF — carries the first TeX error line."""


def render_math_ink(body: str, zoom: float) -> PILImage.Image:
    """Compile ``body`` in a ``standalone`` doc and rasterize the shrink-wrapped page to RGBA ink.

    The page is tight to the math; ``alpha=True`` renders black glyphs on transparent paper, so the alpha
    channel *is* glyph coverage — hand the result to ``recolor`` to paint it in any fg/bg.
    """
    with TemporaryDirectory() as tmp:
        (Path(tmp) / "eq.tex").write_text(STANDALONE % body)
        proc = subprocess.run(
            ["pdflatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "eq.tex"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        pdf = Path(tmp) / "eq.pdf"
        if proc.returncode != 0 or not pdf.exists():
            raise MathRenderError(_first_tex_error(proc.stdout.decode("utf-8", "replace")))
        doc = pymupdf.open(pdf)
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
        ink = PILImage.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        doc.close()
        return ink


def recolor(ink: PILImage.Image, fg: tuple, bg: tuple) -> PILImage.Image:
    """Paint the ink's glyphs in ``fg`` over a solid ``bg`` field, using its alpha as the coverage mask."""
    mask = ink.getchannel("A")
    return PILImage.composite(PILImage.new("RGB", ink.size, fg), PILImage.new("RGB", ink.size, bg), mask)


def _first_tex_error(log: str) -> str:
    for line in log.splitlines():
        if line.startswith("!"):
            return line[1:].strip()[:120]
    return "pdflatex failed"


def _error_bitmap(message: str, fg: tuple, bg: tuple, px: int) -> PILImage.Image:
    """A small bitmap carrying the compile error, so a bad equation degrades to a visible notice, not a hang."""
    text = f"⚠ {message}"
    font = ImageFont.load_default(size=max(10, round(px * 0.7)))
    box = ImageDraw.Draw(PILImage.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)
    img = PILImage.new("RGB", (box[2] - box[0] + 8, box[3] - box[1] + 6), bg)
    ImageDraw.Draw(img).text((4, 3 - box[1]), text, fill=fg, font=font)
    return img


# ========================================================================================================
# MATH SOURCE + BLOCK — one equation, rasterized off-thread by ``Image``, sized to the terminal font
# ========================================================================================================

class MathSource:
    """An ``ImageSource`` that compiles one ``$$ … $$`` body to a theme-colored, font-sized bitmap.

    ``render`` runs on ``Image``'s worker thread, so the whole pdflatex -> pymupdf -> recolor chain stays
    off the event loop. The output size is derived from ``ctx.cell`` (and capped to the message column),
    so the key includes both — a font-zoom or a column resize re-renders, a plain repaint reuses the bitmap.
    """

    def __init__(self, latex: str, fg: tuple, bg: tuple) -> None:
        self._latex = latex.strip()
        self._fg = fg
        self._bg = bg

    def cache_key(self, ctx: graphics.RenderContext) -> object:
        return (self._latex, self._fg, self._bg, ctx.cell.height, ctx.content_width)

    def render(self, ctx: graphics.RenderContext) -> PILImage.Image:
        cell_h = ctx.cell.height
        max_w_px = ctx.content_width * ctx.cell.width
        target = MATH_TEXT_RATIO * cell_h / MATH_PT                  # display px per LaTeX pt
        try:
            ink = render_math_ink(rf"$\displaystyle {self._latex}$", zoom=MATH_SUPERSAMPLE * target)
        except MathRenderError as err:
            return _error_bitmap(str(err), self._fg, self._bg, round(MATH_TEXT_RATIO * cell_h))
        img = recolor(ink, self._fg, self._bg)
        scale = min(1.0 / MATH_SUPERSAMPLE, max_w_px / img.width)    # downsample, never wider than the column
        return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                          PILImage.Resampling.LANCZOS)               # crisp downscale of anti-aliased glyphs


class MathBlock(graphics.Image):
    """A ``$$ … $$`` block: a ``fit="native"`` ``Image`` that reflows its row height to the rendered math.

    Everything hard — the off-thread encode, the "rendering…" placeholder, suppression while clipped by the
    feed's scroll or covered by a float — is the base ``Image``. This adds only the two equation-specific
    bits: a ``MathSource`` built from the live theme colors, and a height reflow once the bitmap lands (the
    block can't know how many rows a multi-line ``cases``/``aligned`` block needs until it's rasterized).
    """

    DEFAULT_CSS = "MathBlock { width: 1fr; height: 1; color: $text; }"

    def __init__(self, latex: str, **kwargs) -> None:
        super().__init__(fit="native", **kwargs)
        self._latex = latex

    def on_mount(self) -> None:
        _, bg = self.background_colors
        color = self.styles.color
        fg = (color.r, color.g, color.b) if color is not None else (235, 235, 235)
        self.show(MathSource(self._latex, fg, (bg.r, bg.g, bg.b)))

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, PILImage.Image):
            cell = graphics.cell_metrics().current                  # reflow to the bitmap's height in cells
            self.styles.height = max(1, math.ceil(event.worker.result.height / cell.height))
        super().on_worker_state_changed(event)


# ========================================================================================================
# $$ … $$ SPLITTING — append-only, so finalized segments never change once formed
# ========================================================================================================

def math_boundary(body: str) -> int:
    """Index just past the last *closed* ``$$ … $$``. Everything before is finalized; the rest is the tail."""
    boundary, i = 0, 0
    while True:
        open_ = body.find("$$", i)
        if open_ == -1:
            break
        close = body.find("$$", open_ + 2)
        if close == -1:
            break
        boundary = i = close + 2
    return boundary


def finalized_segments(prefix: str) -> list[tuple[str, str]]:
    """Split the finalized ``prefix`` (ends on a closing ``$$``) into ``("text"|"math", content)`` pairs."""
    segs, i = [], 0
    while i < len(prefix):
        open_ = prefix.find("$$", i)
        if open_ == -1:
            segs.append(("text", prefix[i:]))
            break
        if open_ > i:
            segs.append(("text", prefix[i:open_]))
        close = prefix.find("$$", open_ + 2)        # guaranteed to exist inside a finalized prefix
        segs.append(("math", prefix[open_ + 2:close]))
        i = close + 2
    return segs


class MarkdownWithMath(Vertical):
    """Stream markdown, but render each closed ``$$ … $$`` as a ``MathBlock``. Replaces a plain ``Markdown``.

    Call ``update_stream(body)`` with the full body each tick. Append-only: finalized segments mount once;
    the trailing ``Markdown`` keeps absorbing the live tail (an unclosed ``$$…`` shows as raw text until it
    closes, then pops into a ``MathBlock``).
    """

    DEFAULT_CSS = "MarkdownWithMath { height: auto; }"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._final_count = 0
        self._tail: Markdown | None = None

    def compose(self) -> ComposeResult:
        self._tail = Markdown("")
        yield self._tail

    async def update_stream(self, body: str) -> None:
        boundary = math_boundary(body)
        segs = finalized_segments(body[:boundary])
        for kind, content in segs[self._final_count:]:          # only the newly-finalized segments
            widget = Markdown(content) if kind == "text" else MathBlock(content)
            await self.mount(widget, before=self._tail)
        self._final_count = len(segs)
        if self._tail is not None:
            self._tail.update(body[boundary:])                  # the live tail — raw text, incl. an unclosed $$


# ========================================================================================================
# DEMO — fake-stream one agent message containing prose + display math, inside a scroll feed
# ========================================================================================================

SCRIPT = r"""## Streaming a message with math

Here's a derivation that arrives a few characters at a time, the way an agent response streams into the
feed. Inline references like the discriminant are written in prose, while display blocks get their own
rendered image. The quadratic formula:

$$ x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a} $$

A couple of paragraphs of filler so the feed actually has to scroll, which is the whole point of the
demo — we want to watch what a sixel block does as it crosses the top and bottom edges of the viewport.
Scrolling is where the graphics API has to earn its keep.

The Basel problem, as a second block to stack below the first:

$$ \sum_{n=1}^{\infty} \frac{1}{n^2} = \frac{\pi^2}{6} $$

And a piecewise definition, to prove ``cases`` survives the round trip from stream to image:

$$ \text{sgn}(x) = \begin{cases} -1 & \text{if } x < 0 \\ 0 & \text{if } x = 0 \\ 1 & \text{if } x > 0 \end{cases} $$

More trailing prose so there's content below the last equation, letting us scroll the final block up
through the viewport and back. Once streaming finishes, use the wheel or arrow keys to move around.
"""


class ChatMathApp(App):
    """Fake-stream ``SCRIPT`` into a ``MarkdownWithMath`` inside a scroll feed; scroll to test clipping."""

    BINDINGS = [("q", "quit", "Quit")]

    # #footer reserves the bottom row: re-emitting a sixel that touches the screen's last line scrolls
    # the terminal (see the graphics DOC.md §5.4).
    CSS = """
    #feed { height: 1fr; }
    MarkdownWithMath { height: auto; padding: 1 2; color: rgb(204, 204, 204); }
    #footer { height: 1; padding: 0 1; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="feed"):
            yield MarkdownWithMath(id="body")
        yield Static("streaming…", id="footer")

    def on_mount(self) -> None:
        asyncio.create_task(self._stream())

    def on_resize(self, event: events.Resize) -> None:
        # Keep the cell size live so math re-rasterizes at the right scale on a font-zoom.
        graphics.note_resize(self.size.width, self.size.height, event.pixel_size)

    async def _stream(self) -> None:
        body = self.query_one("#body", MarkdownWithMath)
        feed = self.query_one("#feed", VerticalScroll)
        n = 0
        while n < len(SCRIPT):
            n = min(len(SCRIPT), n + 3)
            await body.update_stream(SCRIPT[:n])
            feed.scroll_end(animate=False)          # autoscroll like a live chat
            await asyncio.sleep(0.03)
        self.query_one("#footer", Static).update(
            "done  ·  scroll up/down to test the clip behavior · q quits")


def main() -> None:
    # MUST run before the Textual app starts — it probes the terminal while stdin is still ours.
    env = graphics.initialize()
    if env.backend is None:
        sys.exit(f"no terminal graphics backend: {env.reason.value if env.reason else 'unknown'}")
    if shutil.which("pdflatex") is None:
        sys.exit("pdflatex not found (install a LaTeX distribution, e.g. texlive).")

    ChatMathApp().run()


if __name__ == "__main__":
    main()
