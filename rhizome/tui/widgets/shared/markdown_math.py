r"""``MarkdownWithMath`` — a ``Markdown`` drop-in that renders ``$$ … $$`` blocks as sixel images.

A vertical container that streams markdown like ``Markdown`` does, but splits each *closed* ``$$ … $$``
display block out into a sized, theme-colored image (compiled with ``pdflatex``, rasterized off the event
loop by the ``rhizome.tui.graphics`` layer). Append-only: finalized text/math segments mount once; a
trailing ``Markdown`` absorbs the live tail (an unclosed ``$$…`` shows as raw text until it closes).

It exposes the slice of ``Markdown``'s surface the chat feed relies on, so callers can stay polymorphic
across a plain ``Markdown`` and this widget via two module-level factories:

  - ``agent_body_widget(text, **kwargs)`` — build the body widget (this one, or a plain ``Markdown``)
  - ``open_stream(widget)``               — open a ``write(delta)`` / ``stop()`` stream over either widget
  - ``.update(body)``                     — full-body one-shot (the sealed path); safe to call un-awaited

``open_stream`` returns a ``MarkdownMathStream`` whose ``write`` *appends* a fragment (matching Textual's
``MarkdownStream``) and re-runs the append-only reconcile, so an existing delta-based streamer drives it
unchanged. ``agent_body_widget`` picks this widget only when math can actually be rendered (a graphics
backend was selected *and* ``pdflatex`` is on PATH) and a plain ``Markdown`` otherwise — so a machine
without a sixel terminal or LaTeX simply shows ``$$…$$`` as text rather than failing.

The LaTeX rasterization (``render_math_ink`` / ``recolor``) is the *content source* the graphics layer
stays ignorant of; sizing follows ``MATH_RENDERING_NOTES.md`` (math tracks the terminal text height).
"""

from __future__ import annotations

import asyncio
import functools
import math
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream
from textual.worker import Worker, WorkerState

import rhizome.tui.graphics as graphics

# Rendered-math sizing + sharpness (see MATH_RENDERING_NOTES.md). The on-screen text target is
# MATH_TEXT_RATIO × the live cell height; we rasterize at MATH_SUPERSAMPLE × that and downsample, so the
# encoded image is always supersampled relative to its on-screen size. Deriving the size from the live cell
# height is what makes a block re-rasterize sharp — and larger — when the terminal font zooms.
MATH_PT = 12             # LaTeX nominal font pt baked into the standalone doc class
MATH_TEXT_RATIO = 1.20   # rendered math height ÷ terminal text height (1.0 = parity; <1 tighter; >1 bigger)
MATH_SUPERSAMPLE = 2     # rasterize this many × display res, then LANCZOS-downsample -> crisp anti-aliased edges


# ========================================================================================================
# RASTERIZE HALF — LaTeX body -> tight, theme-colored bitmap (the content source, graphics-layer-agnostic)
# ========================================================================================================

# border={<horizontal> <vertical>}: generous left/right + a little top/bottom whitespace, so glyphs never
# reach the bitmap's edge cells (the last cell can get clipped when the blob is blitted tight to its box).
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
# MATH SOURCE + IMAGE — one equation, rasterized off-thread by ``graphics.Image``, sized to the font
# ========================================================================================================

class MathSource:
    """An ``ImageSource`` compiling one ``$$ … $$`` body to a theme-colored, font-sized bitmap off-thread.

    Size is derived from ``ctx.cell`` × ``zoom``; ``max_w_cells`` is an upper bound (so a very wide block
    can't run off the screen) that is deliberately *independent of the image widget's own width* — the
    widget shrinks to fit the rendered equation, and keying the cap to that width would re-compile on every
    reflow. The cache key covers everything the bitmap depends on, so only a real change re-renders.
    """

    def __init__(self, latex: str, fg: tuple, bg: tuple, zoom: float = 1.0, max_w_cells: int = 0) -> None:
        self._latex = latex.strip()
        self._fg = fg
        self._bg = bg
        self._zoom = zoom
        self._max_w_cells = max_w_cells

    def cache_key(self, ctx: graphics.RenderContext) -> object:
        return (self._latex, self._fg, self._bg, self._zoom, self._max_w_cells, ctx.cell.height)

    def render(self, ctx: graphics.RenderContext) -> PILImage.Image:
        cell_h = ctx.cell.height
        max_w_px = self._max_w_cells * ctx.cell.width               # 0 -> no cap
        target = MATH_TEXT_RATIO * cell_h / MATH_PT * self._zoom     # display px per LaTeX pt
        try:
            ink = render_math_ink(rf"$\displaystyle {self._latex}$", zoom=MATH_SUPERSAMPLE * target)
        except MathRenderError as err:
            return _error_bitmap(str(err), self._fg, self._bg, round(MATH_TEXT_RATIO * cell_h))
        img = recolor(ink, self._fg, self._bg)
        scale = 1.0 / MATH_SUPERSAMPLE                              # downsample the supersampled render
        if max_w_px:
            scale = min(scale, max_w_px / img.width)                # never wider than the screen
        return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                          PILImage.Resampling.LANCZOS)               # crisp downscale of anti-aliased glyphs


class MathImage(graphics.Image):
    """The rendered equation: a ``fit="native"`` ``Image`` that shrinks to the bitmap it draws.

    The off-thread encode, the "rendering…" placeholder, and suppression while clipped by an ancestor
    scroll or covered by a float all come from the base ``Image``. This adds the equation-specific parts:
    a ``MathSource`` built from the live theme colors, a 1×/2× zoom toggle, and a reflow of *both* the
    width and height to the rendered bitmap so the widget is the size of the equation (no full-width
    letterbox bars). The bitmap is capped at the screen width, independent of this reflowed width.

    NB: the resize helper must not be named ``_render`` — that shadows ``Widget._render`` (it would return
    ``None`` and crash the line-render path Textual uses for mouse hit-testing).
    """

    DEFAULT_CSS = "MathImage { width: 1fr; height: 1; }"     # color inherited; size reflows to the bitmap

    def __init__(self, latex: str, **kwargs) -> None:
        super().__init__(fit="native", **kwargs)
        self._latex = latex
        self._zoom = 1.0

    def on_mount(self) -> None:
        self._show_source()

    def _show_source(self) -> None:
        _, bg = self.background_colors
        color = self.styles.color
        fg = (color.r, color.g, color.b) if color is not None else (235, 235, 235)
        self.show(MathSource(self._latex, fg, (bg.r, bg.g, bg.b), self._zoom, self.app.size.width))
        cached = self._cached_bitmap()                              # cache hit (e.g. zoom back to 1×): no worker
        if cached is not None:                                      # fires, so reflow now instead of waiting
            self._apply_size(cached)

    def toggle_zoom(self) -> None:
        self._zoom = 2.0 if self._zoom == 1.0 else 1.0
        self._show_source()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, PILImage.Image):
            self._apply_size(event.worker.result)
        super().on_worker_state_changed(event)

    def _apply_size(self, bitmap: PILImage.Image) -> None:
        """Shrink the widget to the bitmap's cell footprint (so it shrinks back when zooming out, too)."""
        cell = graphics.cell_metrics().current
        self.styles.width = max(1, math.ceil(bitmap.width / cell.width))
        self.styles.height = max(1, math.ceil(bitmap.height / cell.height))

    def _cached_bitmap(self) -> PILImage.Image | None:
        """The already-rendered bitmap for the current source, if its rasterize worker has finished."""
        ctx = self._render_context()
        if ctx is None or self._source is None:
            return None
        worker = self._bitmaps.get(self._source.cache_key(ctx))
        return worker.result if worker is not None and worker.state is WorkerState.SUCCESS else None


# ========================================================================================================
# MATH BLOCK — the equation image + a tiny toolbar (copy / zoom), with matching mouse gestures
# ========================================================================================================

class MathButton(Static, can_focus=False):
    """A single-glyph affordance beside a ``MathBlock``. Posts ``Pressed(action)`` on click."""

    DEFAULT_CSS = """
    MathButton { width: 3; height: 1; margin-left: 1; content-align: center middle; color: rgb(110, 110, 110); }
    MathButton:hover { color: white; }
    """

    class Pressed(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, glyph: str, action: str, **kwargs) -> None:
        super().__init__(glyph, **kwargs)
        self._action = action

    def on_click(self, event: events.Click) -> None:
        event.stop()                                    # don't let the click double as a body gesture
        self.post_message(self.Pressed(self._action))


class MathBlock(Horizontal):
    """A ``$$ … $$`` block: the rendered equation with the copy/zoom buttons at its top-right.

    The equation image and the two buttons sit in a horizontal row, centered as a group within the message
    column; the buttons are height-1 so they top-align beside the (taller) equation. They're laid *beside*
    the image, never over it — the graphics layer suppresses the whole sixel if any widget overlaps it. The
    same two actions are bound to mouse gestures on the block: right-click copies, double-click toggles 2×.
    """

    DEFAULT_CSS = "MathBlock { width: 1fr; height: auto; align: center top; }"

    def __init__(self, latex: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._latex = latex

    def compose(self) -> ComposeResult:
        yield MathImage(self._latex, classes="math-image")
        yield MathButton("🗐", "copy")
        yield MathButton("⌕", "zoom")

    def on_math_button_pressed(self, event: MathButton.Pressed) -> None:
        event.stop()
        self._do(event.action)

    def on_click(self, event: events.Click) -> None:
        if event.chain == 2:                            # double-click anywhere on the block -> toggle zoom
            self._do("zoom")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:                           # right-click -> copy
            self._do("copy")

    def _do(self, action: str) -> None:
        if action == "copy":
            self.app.copy_to_clipboard(f"$$ {self._latex.strip()} $$")
            self.notify("Copied LaTeX to clipboard", timeout=2)
        elif action == "zoom":
            self.query_one(MathImage).toggle_zoom()


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


# ========================================================================================================
# THE WIDGET + ITS STREAM — a ``Markdown``-compatible surface over the segment reconciliation
# ========================================================================================================

class MarkdownWithMath(Vertical):
    """Stream markdown, but render each closed ``$$ … $$`` as a ``MathBlock``. A ``Markdown`` drop-in."""

    DEFAULT_CSS = "MarkdownWithMath { height: auto; }"

    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._body = text or ""
        self._final_count = 0
        self._tail: Markdown | None = None
        self._mounted = False

    def compose(self) -> ComposeResult:
        self._tail = Markdown("")
        yield self._tail

    def on_mount(self) -> None:
        self._mounted = True
        if self._body:                              # body set before we were mounted (e.g. the sealed path)
            self._reconcile()

    # -- Markdown-compatible surface -------------------------------------------------------------

    def update(self, body: str) -> None:
        """Replace the whole body in one shot (mirrors ``Markdown.update``; safe to call un-awaited)."""
        self._set_body(body)

    # -- reconciliation --------------------------------------------------------------------------

    def _set_body(self, body: str) -> None:
        self._body = body
        if self._mounted:
            self._reconcile()

    def _reconcile(self) -> None:
        """Mount any newly-finalized segments and refresh the live tail. Append-only and idempotent."""
        boundary = math_boundary(self._body)
        segs = finalized_segments(self._body[:boundary])
        new = segs[self._final_count:]
        if new:
            children = [Markdown(content) if kind == "text" else MathBlock(content) for kind, content in new]
            self.mount_all(children, before=self._tail)     # un-awaited; mount_all preserves list order
            self._final_count = len(segs)
        if self._tail is not None:
            self._tail.update(self._body[boundary:])         # live tail — raw text, incl. an unclosed $$


class MarkdownMathStream:
    """Adapter exposing ``Markdown``'s stream surface (``write(delta)`` / ``stop()``) over a widget.

    Textual's ``MarkdownStream`` *appends* fragments; ``MarkdownWithMath`` reconciles a full body. This
    accumulates the appended fragments and re-runs the append-only reconcile, so a delta-based streamer
    drives it without change.
    """

    def __init__(self, widget: MarkdownWithMath) -> None:
        self._widget = widget
        self._body = widget._body or ""
        self._stopped = False

    async def write(self, delta: str) -> None:
        if self._stopped:
            raise RuntimeError("Can't write to the stream after it has stopped.")
        if not delta:
            return
        self._body += delta
        self._widget._set_body(self._body)
        await asyncio.sleep(0)                       # yield so the mounts/repaints flush between writes

    async def stop(self) -> None:
        self._stopped = True
        self._widget._set_body(self._body)
        await asyncio.sleep(0)


# ========================================================================================================
# FACTORY — pick the math-capable widget only when math can actually be rendered
# ========================================================================================================

@functools.cache
def _have_pdflatex() -> bool:
    return shutil.which("pdflatex") is not None


def agent_body_widget(text: str = "", **kwargs) -> Markdown | MarkdownWithMath:
    """A ``Markdown`` body, upgraded to ``MarkdownWithMath`` when a backend is selected and LaTeX is present.

    Falls back to a plain ``Markdown`` otherwise, so a terminal without sixel support (or a machine without
    ``pdflatex``) simply shows ``$$…$$`` as literal text instead of failing or drawing error tiles.
    """
    if graphics.active_backend() is not None and _have_pdflatex():
        return MarkdownWithMath(text, **kwargs)
    return Markdown(text, **kwargs)


def open_stream(widget: Markdown | MarkdownWithMath) -> MarkdownStream | MarkdownMathStream:
    """Open a ``write(delta)`` / ``stop()`` stream over an agent-body widget, whichever type it is."""
    if isinstance(widget, MarkdownWithMath):
        return MarkdownMathStream(widget)
    return Markdown.get_stream(widget)
