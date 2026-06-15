# `rhizome/tui/graphics`

Reusable primitives for drawing a **bitmap with hover-selectable regions** in the terminal. The layer
is deliberately **content-dumb**: nothing here knows about PDFs, pages, or resources. It is handed a
bitmap plus box rectangles (in image-pixel space) and renders them through a terminal graphics protocol.
PDF rasterization, flashcard images, MathJax renders, etc. are *sources* that plug in above.

> The hand-written, human-facing explainer for the terminal mechanics underneath this — the query
> "dance", cell-size detection across local/ssh, the sixel format, the coordinate frames — is **`DOC.md`**.
> Read it to understand *why*; this file is the *what*. (`DOC.md` is maintainer-authored; don't edit it
> without an explicit request.)

## The usage contract

`graphics.initialize()` is called **once, by the consumer, in the app entry point, before Textual
starts** — it runs the terminal queries, which need raw stdin while it is still ours (Textual's stdin
reader would otherwise eat the replies). It populates a process-global ``GraphicsEnvironment`` (selected
backend, or a structured failure reason, plus the cell-size resolver). Widgets read that singleton, so
nothing threads a backend around. Pass `Image(backend=…)` only to override detection (tests /
forcing a protocol). Wire `graphics.note_resize(self.size.width, self.size.height, event.pixel_size)`
from the app's `on_resize` to keep the cell size live.

## The two groups

**`terminal/`** — the "dance": discovering what the terminal can do, before the UI takes over.
- **`query.py`** — the substrate. ONE raw-mode excursion (`exchange`: write a payload, read until a
  terminator or a silent timeout), the `TIOCGWINSZ` ioctl (`tiocgwinsz`), `over_ssh()`, and tmux
  passthrough. The only place the library touches raw stdin; works only pre-Textual (see the contract).
- **`probe.py`** — the single probe. Sends `16t;14t;18t` + DA1 together and reads until the DA1 reply
  (the "all replies in" sentinel), returning a `TerminalProbe` (cell-size replies + `supports_sixel`).
  One excursion, one timeout, one failure mode (no DA1 reply ⇒ not a responsive graphics terminal);
  everything downstream is pure interpretation of it.
- **`cellsize.py`** — px-per-cell, the one number bridging cells and pixels. `resolve(probe)` picks it in
  a documented priority (XTWINOPS `16t` → `14t/18t` → ioctl **only off ssh** → env → a NOT-confident
  VT340 fallthrough), and `CellMetrics.update_from_pixels` is the live update fed from resize events.
  `CellMetrics.confident` is False only for the VT340 guess.
- **`capabilities.py`** — `GraphicsUnavailable` (the structured why-not reasons) plus `encoder_available`
  (img2sixel **or** numpy — img2sixel is a speed optimization, not a requirement).

**`render/`** — the frontend: bitmap + boxes → pixels on screen.
- **`geometry.py`** — the letterbox transform, pure math. `placement()` fits a bitmap into a content
  box keeping aspect ratio; `footprint()` inverts it (mouse cell → image px); `first_hit()` picks the
  box under a cell. One transform, used for both draw and hit-test, so they can't drift.
- **`backend.py`** — `GraphicsBackend`, the protocol-neutral contract: `prepare` → `encode` →
  `encode_highlight(frame, rect, style)` → `compose(frame, overlays, …)`. `EncodeJob`/`EncodedFrame`/
  `Highlight` are opaque per-backend; `Outline`/`Fill` are the overlay styles (border vs translucent
  wash). A backend is a *pure renderer* — no `available()`; usability is decided in `terminal`/`environment`.
- **`sixel.py`** — `SixelBackend`. Encodes the letterboxed page once via `img2sixel` when present, else a
  numpy-vectorized pure-Python encoder (`_encode_sixel_py`) — so sixel works without the binary, just
  slower. An overlay is its cell-aligned region cropped from the same canvas, painted per `style` (border
  or wash), blitted after the page so the terminal draws it on top (opaque — no transparency/z-index).
  `blob_strip` is the one strip shape whose escapes survive the compositor's cuts; all emission goes through it.
- **`kitty.py`** — `KittyBackend`, a stub until it can be validated on a real TGP terminal.
- **`source.py`** — `ImageSource` (`cache_key` + `render`), the "where a bitmap comes from" abstraction so
  rasterization can run off-thread (e.g. compile LaTeX → rasterize). `StaticSource` wraps a plain bitmap
  (resolved inline, no worker); `as_source` coerces. `RenderContext` carries the layout a source may need.
- **`image.py`** — `Image`, the base widget. Reads the environment for backend + cell size; runs the
  **two-stage off-thread pipeline** (rasterize → encode), each cached; suppresses its blob when not a
  clean full-rect paint — under another screen (`is_active`), under a same-screen float (`first_occluder`),
  or partially clipped by an ancestor scroll (`visible_region != region`). No interaction; subclasses add
  overlays via the `_overlays_for` hook. Also home to the `Throttle` mixin (coalesce expensive repaints,
  latest-wins) shared by the hover and selection widgets.
- **`overlays.py`** — `ImageWithOverlays(Throttle, Image)`, adds `(rect, payload)` regions, mouse
  hit-testing, the `(job, rect, style)` overlay-tile cache (+ a hover-only pre-encode), hover-outlining,
  and `RegionHovered`/`RegionSelected`.
- **`select.py`** — the `SelectionModel` mixin (drag anchor→focus word range, per-line run-merging, the
  drag handlers, `SelectionChanged` — pure, viewport-independent; delegates `_word_at` + tile rendering to
  the host) and `SelectableImage(SelectionModel, ImageWithOverlays)`, the fit-whole selector: `Fill` tiles,
  hit-test via the inherited `placement`. `Word(rect, text, block, line)` is the unit.
- **`scroll.py`** — `ScrollImage(ScrollView)`: a pan/zoomable image with a pinned cell footprint that
  re-encodes a viewport-window per scroll offset (off-thread, latest-wins, LRU, stale-until-ready). It
  talks to the sixel encoder directly (its window model doesn't fit the `encode`/`compose` contract) — so
  it's **sixel-specialized for now**; a kitty window path comes when that backend lands. Takes a bitmap.
  `_overlay_blobs` is its hook for canvas-absolute overlays (used by the scroll selector).
- **`scroll_select.py`** — `ScrollSelectableImage(SelectionModel, Throttle, ScrollImage)`: the two axes
  married. Selection state in image px (survives zoom/scroll); tiles encoded in canvas-absolute cells and
  *clipped to the viewport window* (a straddling run caches a clipped variant; zoom's epoch retires all);
  hit-test inverts viewport→canvas→image px. Sixel-specialized like `ScrollImage`.

**`environment.py`** — the orchestrator and singleton (`initialize`, `note_resize`, the accessors).
Lifted to the package root because it ties both groups together (it imports concrete backends and the
resolver); the two groups themselves stay one-directional (`terminal` knows nothing of `render`).

## Seams

- **Cell size is the only cell↔pixel constant.** Everything Textual gives (content_size, visible_region,
  mouse offset) is in cells; `cellsize` is the single px multiplier. Get it wrong and every image is
  mis-sized and every hit-test skewed by the same factor — which is why the seed cascade is so careful.
- **Threading lives in the widget, not the backend.** Backends and sources are pure functions; `Image`
  owns two bounded future-caches — `cache_key → rasterize Worker` and `EncodeJob → encode Worker` (the
  latter crop-independent, so probe and paint share it) — draws a "rendering…" placeholder on a miss, and
  `refresh()`es when a worker resolves.
- **`compose` takes the widget's screen `region`** because graphics protocols place output by absolute
  position, not relative to the strip.
- **No more `textual_image` dependency.** This package owns its terminal I/O end to end. The frozen
  prototypes live in `rhizome/tui/graphics_prototype` and still use `textual_image` independently.
