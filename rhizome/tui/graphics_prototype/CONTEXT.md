# `rhizome/tui/graphics_prototype`

Reusable primitives for drawing a **bitmap with hover-selectable regions** in the terminal. The layer
is deliberately **content-dumb**: nothing here knows about PDFs, pages, or resources. It is handed a
bitmap plus box rectangles (in image-pixel space) and renders them through a terminal graphics
protocol. PDF rasterization, flashcard images, MathJax renders, etc. are *sources* that plug in above.

## The pieces

- **`geometry.py`** — the letterbox transform, pure math (no PIL, no Textual). `placement()` fits a
  bitmap into a content box keeping aspect ratio; `footprint()` inverts it (mouse cell -> image px)
  for hit-testing; `first_hit()` picks the box under a cell. Computing the transform in one place is
  what keeps the rendered page and the boxes hit-tested against it from drifting apart.
- **`backend.py`** — `GraphicsBackend`, the protocol-neutral contract: `prepare` (main-thread
  snapshot into a hashable `EncodeJob`) → `encode` (heavy, pure, worker-safe) → `encode_highlight`
  (one box's overlay) → `compose` (build paint `Strip`s, compositing the overlay). `EncodeJob` /
  `EncodedFrame` / `Highlight` are **opaque, per-backend** — that opacity is what lets a new protocol
  slot in without touching anything above. `select_backend()` detects + picks one, or returns None to
  reject (no half-cell fallback). Detection queries the terminal, so call it **at startup** — it also
  pins px-per-cell geometry from the emulator's own XTWINOPS reply, which beats the pty's TIOCGWINSZ
  pixel fields (fake over ssh: Windows OpenSSH stuffs in a hardcoded 640x480).
- **`sixel.py`** — `SixelBackend`, the implemented protocol (libsixel `img2sixel`). Encodes the
  letterboxed page once; a hover overlay is its cell-aligned region cropped from the same canvas with
  a border, blitted after the page sixel so the terminal draws it on top (opaque — no transparency or
  z-index). `blob_strip` is the one strip shape whose escapes survive the compositor's cuts (the
  moment a scrollbar or sibling divides the image's rows) — all sixel emission goes through it. See
  the module docstring for the protocol's quirks.
- **`kitty.py`** — `KittyBackend`, a stub behind `available() -> False` until it can be validated on a
  real TGP terminal. TGP composites overlays by z-index, so its `encode`/`compose` will diverge.
- **`widget.py`** — `GraphicsImage`, the Textual widget that drives a backend: it owns the future-cache
  (below), hit-tests the mouse against the regions, and posts `RegionHovered` / `RegionSelected`. It is
  content-agnostic — `show(bitmap, regions)` where each region is `(rect_in_image_px, payload)`. The
  payloads are opaque; an owner interprets them. `prefetch(bitmap)` warms a neighbour frame. Pixels
  have no z-order, so the widget suppresses its blob when it isn't front-most — under another screen
  (`is_active`) or a same-screen float like a toast/tooltip (`first_occluder`); the image hides and
  repaints from cache when the overlay leaves.

## Seams

- **Threading lives in the widget, not the backend.** The backend is pure functions. `GraphicsImage`
  owns a bounded `dict[EncodeJob, Worker]` future-cache: `render_lines` builds the job, dispatches
  `encode` on a worker if not already in flight (idempotent — the same crop-independent `EncodeJob`
  keys both probe and paint, so the cache never thrashes), draws a "rendering…" placeholder on a miss,
  and `refresh()`es when the worker resolves so the next paint is a cache hit. Once a frame lands, a
  second worker pre-encodes *every* region's hover overlay (cached by `(job, rect)`) so hovering is a
  pure cache hit; a region hovered before that finishes falls back to one cheap synchronous encode.
- **`compose` takes the widget's visible `region`** (screen coords) because graphics protocols place
  output by absolute position, not relative to the strip.
- **Single point of `textual_image` coupling.** We reuse only its terminal queries — `get_cell_size`
  (whose cache `_seed_cell_size` pins), `capture_terminal_response`, and
  `renderable.{sixel,tgp}.query_terminal_support` — and own encoding + placement ourselves. Pin the
  version; that surface is small and terminal-protocol-stable.
