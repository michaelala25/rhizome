# Terminal Graphics: How It Actually Works

> **For future agents:** This is a hand-written explanatory document, authored *for the human maintainer*
> as a from-first-principles reference on the terminal mechanics this library rests on. It is not a
> generated `CONTEXT.md` and it is not auto-maintained. **Do not edit, "freshen", restructure, or
> regenerate this file unless the maintainer explicitly asks you to.** If the code drifts from what is
> written here, surface the discrepancy in conversation rather than silently rewriting the prose. The
> at-a-glance, agent-facing summary of this directory lives in `CONTEXT.md`; keep that one current
> instead.

---

This document explains the low-level terminal magic underneath `rhizome/tui/graphics_prototype`. The goal is that
after reading it you can answer, for any new terminal/SSH/tmux situation: *what does it actually take to
put pixels on this screen and know where they landed?* Everything is grounded in the real code in this
directory, but the bulk of the text is the "why", because the "why" is where all the sharp edges live.

The four things it covers, in order:

1. **The substrate** — how a program asks a terminal *anything* (the query/reply dance). Both backend
   detection and cell-size detection are built on this, so it comes first.
2. **Backend detection** — how we decide whether this terminal can do sixel, kitty/TGP, or nothing.
3. **Cell size** — how we learn how many *pixels* a character cell is, across local / SSH / tmux, and
   why this single pair of numbers is the linchpin of the whole library.
4. **Coordinate frames** — every coordinate space involved, what data pins each one, and the exact
   forward (draw) and inverse (hit-test) transforms.
5. **Sixel** — what a sixel literally *is*, byte by byte, and why its one defining limitation (pixels
   have no z-order) dictates nearly every design decision above it.

It closes with a checklist: *the minimum set of facts and capabilities required to blit into an arbitrary
sixel terminal.*

---

## 0. The mental model

A terminal emulator is, at heart, a **grid of character cells**. The grid is `rows × columns`. Each cell
is a fixed rectangle of pixels — say 10 wide by 20 tall — sized by the font. Historically a program could
only ask the terminal to put a *glyph* (a character, in some color) into a cell. It could not address
individual pixels.

**Graphics protocols** (sixel, kitty's Terminal Graphics Protocol) are escape-sequence extensions that
let a program paint actual pixels into that grid, ignoring the character abstraction. This library is the
machinery for doing that *well* inside a Textual app.

Two facts about a terminal drive everything that follows:

- **A program talks to the terminal over two byte streams.** `stdout` carries bytes *to* the terminal
  (text to display, escape sequences = commands). `stdin` carries bytes *from* the terminal to the
  program (normally the user's keystrokes — but, crucially, also *replies* to certain queries; see §1).

- **There are two coordinate worlds that don't match.** Textual thinks in **cells** (the mouse arrives at
  "column 12, row 4"; a widget is "80 cells wide"). Sixel thinks in **pixels** ("paint this 1280×720
  bitmap here"). The only bridge between them is one pair of numbers — *how many pixels is one cell* — and
  obtaining that pair reliably is the single hardest plumbing problem in this whole library (§3). Get it
  wrong and *every* image is the wrong size and *every* mouse hit-test is skewed by the same factor.

Hold those two facts in mind; the rest is detail.

---

## 1. The substrate: how a program asks the terminal anything

Almost all the "magic" in terminal graphics is one trick: **you can write a query to stdout, and the
terminal writes its answer back onto your stdin**, as if the user had typed it. This is how a program
discovers, at runtime, things only the terminal knows — "do you support sixel?", "how big is a cell?",
"what kind of terminal are you?".

This sounds simple and is full of traps. Here is the dance in full, because both §2 and §3 depend on it
working *exactly right*.

### 1.1 Why it needs "raw mode"

Normally a terminal runs in **cooked / canonical mode**: stdin is line-buffered (the program sees nothing
until Enter is pressed) and echoed (typed characters are shown). That is perfect for typing commands and
useless for reading a machine reply, which arrives as a burst of control characters with no newline.

So to read a reply you must temporarily switch the terminal into **raw** (or **cbreak**) mode: characters
become available to the program immediately, one at a time, with no echo and no line buffering. After the
read you must restore the original mode, or you'll wreck the user's shell.

In this codebase that switch is `textual_image`'s `capture_mode()` context manager
(`_posix.py`), which does exactly `tty.setcbreak(stdin)` on entry and `termios.tcsetattr(...)` to restore
on exit. The diagnostic `probe_geometry.py` does the same thing by hand with `tty.setraw()` so you can
see the bytes.

### 1.2 The dance

```
1. put stdin into raw/cbreak mode          (capture_mode)
2. write the query escape sequence to stdout, and FLUSH
3. read stdin byte-by-byte until you see the reply's terminator,
   OR until a short timeout elapses with no data
4. restore the original terminal mode
```

Step 3's **timeout is not optional**. If the terminal does *not* support the query, it sends nothing at
all — there is no "unsupported" reply. Without a timeout you would block forever. The timeout (typically
0.1–0.5 s here) is the only signal that means "this terminal can't answer that". This is why
`capture_terminal_response` reads with `select([fd], [], [], timeout)` and raises `TimeoutError` on
silence — and why "no reply" is treated as "feature absent" everywhere in this library.

`capture_terminal_response(start_marker, end_marker, timeout)` in `textual_image/_terminal.py` is the
canonical implementation. It accumulates stdin one character at a time until the buffer ends with
`end_marker`, and it validates each prefix against `start_marker` so a stray keystroke that doesn't look
like the expected reply raises rather than corrupting the result.

### 1.3 The two flakiness hazards (real, accepted)

The upstream docstrings are blunt about this and worth internalizing:

- **A keystroke during the read can be mistaken for the reply.** If the user is typing exactly when we
  query, their byte lands on stdin interleaved with (or instead of) the terminal's answer. We accept this
  — it's improbable in the startup window where we do all our querying.
- **If no reply comes, the first byte of *real* stdin can be eaten.** The reader grabs one character to
  decide whether it's the start of a reply; on a no-support terminal that character might be the user's
  first keystroke, silently consumed. Again: improbable at startup, accepted.

### 1.4 The hard constraint: this only works *before* Textual starts

This is the single most load-bearing operational fact in the library.

Textual, once running, **spawns its own thread that reads stdin** (to deliver key/mouse/paste events).
That thread will grab the terminal's reply to our query before our code ever sees it. So every
query/reply — backend detection *and* cell-size detection — **must happen at startup, before the Textual
app takes over stdin.** After that, the channel is gone.

This is why:

- `select_backend()` is documented "call once at startup" and is the single entry point that runs *all*
  the startup terminal queries (`available()` for each backend, and `_seed_cell_size()`), see
  `backend.py`.
- `_seed_cell_size()` *pins* its answer into `get_cell_size`'s cache (§3.4). It cannot rely on a later,
  in-widget lazy lookup self-correcting, because by then Textual's reader owns stdin and the reply would
  never arrive. The lazy lookup would silently fall through to a wrong default forever.

> Rule of thumb: **anything that needs to hear the terminal talk back must run in `select_backend()`,
> pre-app.** If you find yourself wanting to query the terminal from inside a widget, you can't — the
> answer has to be captured at boot and threaded in.

---

## 2. Determining the available backend (sixel / kitty / none)

"Is this terminal capable of graphics protocol X?" is answered with the §1 dance. The two protocols use
different queries; both are wrapped by `textual_image` and called from each backend's `available()`.

### 2.1 Sixel: the Primary Device Attributes (DA1) query

`textual_image.renderable.sixel.query_terminal_support()` (used by `SixelBackend.available()`) sends:

```
ESC [ c          (written as "\x1b[c")
```

This is **DA1 — Primary Device Attributes** — the ancient "what are you?" query every VT-style terminal
answers. The reply looks like:

```
ESC [ ? 62 ; 4 ; 6 ; 9 ; 22 c
```

The numbers between `?` and `c` are a semicolon-separated list of capability codes. **Code `4` means
"sixel graphics".** So the entire detection is: send `ESC[c`, read until `c`, split on `;`, check whether
`"4"` is in the list. That's the whole thing — see `query_terminal_support` returning
`"4" in sequence.split(";")`.

### 2.2 Kitty/TGP: send a tiny image, expect "OK"

`textual_image.renderable.tgp.query_terminal_support()` works differently: it transmits a 1-pixel TGP
image with a query flag and waits for the protocol's `...;OK` acknowledgement. If the terminal isn't
TGP-capable, nothing comes back (timeout → not supported). Our `KittyBackend` is currently a stub
(`available()` hardcoded to `False`) until it can be validated on a real TGP terminal — see `kitty.py`.

### 2.3 Our `available()` has *two* gates, not one

Terminal support is necessary but not sufficient. `SixelBackend.available()` checks **both**:

```python
if shutil.which("img2sixel") is None:        # gate 1: the encoder binary exists
    return False
return _sixel.query_terminal_support()        # gate 2: the terminal can display sixel
```

We encode sixel by shelling out to libsixel's `img2sixel` CLI (§4.6), so a terminal that supports sixel
is still useless to us if that binary isn't installed (`apt install libsixel-bin`). Both must be true.

### 2.4 Selection policy: best available, or *nothing*

`select_backend()` tries backends in order (`SixelBackend`, then `KittyBackend`) and returns the first
whose `available()` is true, or `None`. The order is **not** a quality judgement — sixel is first only
because it's the implemented one today.

The deliberate design choice worth flagging: **there is no half-cell / Unicode-block fallback.** If no
true graphics backend is available, `select_backend()` returns `None` and the widget shows a plain
"terminal graphics unavailable" notice (`GraphicsImage.render_lines` → `_notice`). We would rather show
nothing than degrade to chunky block-character approximations. If you ever want a fallback renderer, it
belongs as another `GraphicsBackend` subclass, slotted into the `select_backend` order — not as a special
case bolted onto the widget.

---

## 3. Cell size: the one pair of numbers everything hinges on

To draw or hit-test anything we must know **how many pixels wide and tall a single character cell is**.
Textual gives us geometry in cells; sixel needs pixels; cell size is the multiplier between them. This
section is the longest because obtaining it correctly is genuinely hard, and getting it wrong fails
*silently* — the image just renders tiny and misaligned with no error.

There are several possible sources, of varying trustworthiness. `textual_image.get_cell_size()` tries
them in a cascade; `_seed_cell_size()` (ours) intervenes to override that cascade when it's about to
trust a lie. Let's take the sources one at a time.

### 3.1 Source A — `TIOCGWINSZ` (the kernel ioctl)

**What it is.** `TIOCGWINSZ` = "Terminal I/O Control: Get WINdow SiZe". An **ioctl** is a system call
that asks a device driver for information about a device. A terminal program isn't connected to a real
serial terminal anymore; it's connected to a **pty** (pseudo-terminal) — a kernel device that pretends to
be a terminal. The pty driver holds a small `winsize` struct, and `TIOCGWINSZ` reads it:

```c
struct winsize {
    unsigned short ws_row;     // rows    (characters)
    unsigned short ws_col;     // columns (characters)
    unsigned short ws_xpixel;  // total text-area width  in PIXELS
    unsigned short ws_ypixel;  // total text-area height in PIXELS
};
```

In Python: `fcntl.ioctl(fd, termios.TIOCGWINSZ, buf)` fills a 4×`unsigned short` buffer with
`(rows, cols, xpixel, ypixel)` — exactly what `textual_image._posix.get_tiocgwinsz()` and
`probe_geometry.py` unpack. From it, `get_cell_size` computes `cell_w = xpixel / cols`,
`cell_h = ypixel / rows`.

**The crucial question: who fills in `ws_xpixel` / `ws_ypixel`?** The *terminal emulator* does, when it
creates the pty and on every resize. And here's the rub:

- `ws_row` / `ws_col` are essentially always correct — every terminal maintains them.
- `ws_xpixel` / `ws_ypixel` are **optional**, and many terminals never set them. When unset they read as
  **zero**. Zero is *honest*: `get_cell_size` sees a zero and falls through to the next source. Fine.
- But over SSH they can be **non-zero and wrong** — and that's the trap.

### 3.2 The SSH trap: "sensible garbage"

When you SSH into a host, the pty is created *on the server*. The SSH **client** is the only thing that
knows the real window pixel geometry, and it forwards `rows/cols/xpixel/ypixel` to the server over the
SSH protocol (in the `pty-req` request and `window-change` messages). The server stuffs those numbers
into the server-side pty's `winsize`. So `TIOCGWINSZ` on the server returns *whatever the client chose to
send*.

A well-behaved client that knows its font geometry sends correct pixel fields. **But some clients send a
hardcoded lie.** The case this library hit: **Windows OpenSSH client → WSL `sshd`**. Windows OpenSSH (via
ConPTY) has no concept of pixel geometry, so rather than send zero it sends a **fixed `640 × 480`**
regardless of the actual window. With, say, a 320-column × 80-row grid that yields:

```
cell_w = 640 / 320 = 2 px      cell_h = 480 / 80 = 6 px
```

A ~2×6 px cell. Nonsense — but **non-zero**. And `textual_image.get_cell_size()` *trusts any non-zero
ioctl result* (it only falls through on zero). So over this SSH path it confidently computes a ~2×6 cell,
and then:

- every image is letterboxed into a box measured as `cols×2 by rows×6` pixels → tiny;
- every cell↔pixel transform (placement, hit-test, overlay tile positions) is skewed by the same wrong
  factor → misaligned.

No exception, no warning — it just looks broken. This is the "sensible garbage" problem: **a value that
is plausible enough to be trusted and wrong enough to break everything.** It's the reason `_seed_cell_size`
exists.

### 3.3 Source B — XTWINOPS escape queries (the SSH-proof source)

The fix is to ask the source that actually knows and is reachable even over SSH: **the terminal emulator
itself**, via escape-sequence queries. These belong to the **XTWINOPS** family — xterm's "window
operations", the `CSI Ps ; Ps ; Ps t` sequences. (Sometimes called "xwinops" — same thing: XTerm WINdow
OPerationS.) The relevant query codes:

| Send (stdout) | Meaning of the query        | Reply (stdin)            | Gives us                |
|---------------|-----------------------------|--------------------------|-------------------------|
| `CSI 14 t`    | text-area size in **pixels**| `CSI 4 ; H ; W t`        | total area px           |
| `CSI 16 t`    | **cell size** in **pixels** | `CSI 6 ; H ; W t`        | px per cell *directly*  |
| `CSI 18 t`    | text-area size in **chars** | `CSI 8 ; H ; W t`        | rows / cols             |

(`CSI` = `ESC [`. Note the reply codes are the query code minus 10: 14→4, 16→6, 18→8.)

So:

- `CSI 16 t` hands back the cell size **directly** — the best case. `_xtwinops(16)` in `backend.py`.
- If `16t` is unsupported, derive it: `cell = (area_px from 14t) / (area_chars from 18t)`. This is exactly
  the `_seed_cell_size` fallback: `cell = (area[0] // chars[0], area[1] // chars[1])`.

**Why these are reliable over SSH, when `TIOCGWINSZ` isn't.** The query and its reply are *ordinary
terminal I/O*. `CSI 16 t` is bytes the program writes to stdout; the emulator's reply is bytes that flow
back over stdin. Over SSH that round trip rides the SSH data channel like any other input/output — **the
kernel pty's `winsize` and SSH's window-size signaling are entirely out of the loop.** The answer comes
straight from the program that will actually draw the pixels, so it is authoritative *by construction*:
even if the number is a "virtual" geometry (see §3.5), it's the same geometry the emulator will use when
it places our sixels. **Placement only needs internal consistency, and asking the renderer directly is
how you guarantee it.**

### 3.4 Our intervention: `_seed_cell_size()`

`textual_image.get_cell_size()`'s built-in cascade is: ioctl → `CSI 16 t` → env vars → VT340 default. The
problem is step 1 *wins on the SSH garbage* before step 2 ever runs. So we pre-empt it.

`_seed_cell_size()` (in `backend.py`, called by `select_backend()` at startup) does:

1. Query `CSI 16 t`; if that fails, derive from `CSI 14 t` + `CSI 18 t`.
2. If we got a sane (`> 0`) pair, **pin** it as `textual_image`'s cached answer:
   `setattr(get_cell_size, "_result", CellSize(...))`.

`get_cell_size` checks `if hasattr(get_cell_size, "_result")` *first* and returns the cached value
without running its cascade at all. By stuffing the cache before anything else calls `get_cell_size`, we
force the authoritative XTWINOPS answer to win over the fake ioctl. If **no** escape reply comes (a
terminal with no XTWINOPS support — typically a plain local terminal where the ioctl is actually
correct), we leave the cache untouched and let the normal cascade run. So the override only fires when we
have something strictly better.

Two timing facts make this work, both from §1.4:

- It must run **pre-Textual** (it uses the raw-mode query/reply, which Textual's stdin reader would
  otherwise steal).
- It must run **before any other `get_cell_size()` call**, because that function caches its first result
  forever. `select_backend()` ordering guarantees this.

### 3.5 The VT340 fallback, virtual pixels, and why SSH looks soft

If every source fails, `textual_image` assumes **VT340** geometry: `10 × 20` px/cell. The VT340 is the
historical DEC sixel terminal; its geometry is the genre's lingua franca.

A curious thing observed over the Windows-Terminal-via-SSH path: the *correct* `CSI 16 t` answer there is
**also exactly 10×20**. Windows Terminal presents a **virtual VT340 pixel grid** and scales sixels to its
real font cell. So the seeded answer is "right" (consistent — placement and hit-testing line up perfectly,
because 16t comes from the same program that blits), but it **caps effective resolution at 10×20 px/cell**
over that client. That's why pages look softer over that SSH path than locally: not a bug, a property of
the client's virtual grid, and not fixable server-side.

### 3.6 Known open edge: physical-pixel terminals and font zoom

`get_cell_size` caches its answer for the process lifetime. A terminal that reports **physical** pixels
(real font cell size, not a virtual grid) will report a *different* cell size after the user zooms the
font — but we've already cached the old one, so images go stale (wrong size).

- Over the virtual-grid SSH client this happens *not* to bite: probing at two zoom levels showed `CSI 16 t`
  stays 10×20 while the grid reflows — the client's px-per-cell coordinate system is zoom-invariant.
- Locally on a physical-px terminal it is a real, open limitation. The clean fix is Textual's **in-band
  resize (mode 2048)**: a capable emulator reports its pixel size with each resize event
  (`Resize.pixel_size`), populated *only* from the emulator (the SIGWINCH fallback posts `None`, never the
  fake ioctl px), so we could re-pin the cache on `on_resize`. It needs a mode-2048-capable terminal to
  build against, so it's deferred. (`GraphicsImage` already rebuilds its `EncodeJob` per paint, so a
  genuine `content_size` change is a natural cache miss — what's missing is *noticing* the px-per-cell
  change when only the font zoomed and the cell count didn't.)

---

## 4. Coordinate frames and the transforms between them

With cell size in hand, the geometry is just arithmetic — but there are several coordinate spaces and it's
easy to lose track of which one a given number lives in. Here they all are. The pure math lives in
`geometry.py`; the widget (`widget.py`) and the sixel backend (`sixel.py`) apply it.

### 4.1 The frames

```
 ┌────────────────────┐  ┌──────────────────────────┐  ┌─────────────────────┐  ┌──────────────────┐
 │ A. IMAGE PIXELS     │  │ B. BOX / CANVAS PIXELS    │  │ C. WIDGET CELLS     │  │ D. SCREEN CELLS  │
 │ the source bitmap   │  │ the content box, in px,   │  │ cells relative to   │  │ absolute cells   │
 │ (a rasterized PDF   │  │ with the image scaled +   │  │ the widget's top-   │  │ in the whole     │
 │  page). Region      │  │ letterboxed onto a bg     │  │ left. The mouse     │  │ terminal grid.   │
 │  rects (text blocks)│  │ canvas. THIS is what gets │  │ arrives here        │  │ Where the blob   │
 │  & selection state  │  │ sixel-encoded.            │  │ (event.offset).     │  │ is placed.       │
 │  live here.         │  │                           │  │                     │  │                  │
 └────────────────────┘  └──────────────────────────┘  └─────────────────────┘  └──────────────────┘
```

**A. Image pixels.** The bitmap's own pixel grid, origin top-left, extent `bitmap.width × bitmap.height`.
A PDF page rasterized at some zoom lives here. The hover/selectable region rectangles (`(x0,y0,x1,y1)`
text-block boxes) are expressed here, and so is selection state in the scroll prototypes — *deliberately*,
because image-px coordinates are invariant under both terminal zoom and scrolling.

**B. Box / canvas pixels.** The widget's content box, measured in **pixels**:
`box_w = content_size.width × cell_w`, `box_h = content_size.height × cell_h`. The source image is scaled
to fit (aspect-ratio-preserving) and pasted at an offset onto a background-colored canvas of exactly this
size; the leftover margins are the letterbox bars. **This canvas is the thing that gets encoded to
sixel.** Sizing the box in whole cells (× cell size) means the encoded blob lands exactly on cell
boundaries.

**C. Widget cells.** Terminal cells relative to the widget's own content origin. Textual delivers the
mouse here: `on_mouse_move(event)` → `event.offset.(x,y)` is a widget-relative cell coordinate.

**D. Screen cells.** Absolute cell coordinates in the full terminal grid — the widget's `visible_region`
as the compositor sees it. Graphics protocols place output by **absolute** position (you move the cursor
to an absolute row/col and emit the blob there), *not* relative to the strip being painted. This is why
`backend.compose(...)` takes `region` (the widget's screen-absolute region) and emits the sixel with
`Control.move_to(region.x, region.y)`.

### 4.2 What data pins each frame

This is the practical core of the section — *what must you know to describe each space?*

| Frame | Pinned by |
|-------|-----------|
| A. image px | the bitmap itself (its width × height) |
| B. box px   | `content_size` (cells, from Textual) **× cell size (px/cell)**, plus the `Placement` (scale + offsets), which depends on image aspect vs box aspect and `halign` |
| C. widget cells | **cell size (px/cell)** |
| D. screen cells | the widget's `visible_region` (cells, from the compositor) |

Notice that **the only quantity that ever converts cells↔pixels is the cell size.** Everything Textual
hands us (`content_size`, `visible_region`, the mouse offset) is in cells; the *single* px constant in the
entire pipeline is `cell_w × cell_h`. That is precisely why §3 matters so much: one wrong cell size scales
*and* skews every frame B/C transform by the same factor, which is the exact signature of the SSH bug.

### 4.3 The forward (draw) transform

```
 image px ──(× scale, + (off_x, off_y))──▶ box/canvas px ──(encode)──▶ sixel ──(place at screen region)──▶ pixels
            └────────── Placement ───────┘
```

`placement(img_w, img_h, box_w, box_h, halign)` in `geometry.py` computes the whole forward map once and
returns it as a `Placement`:

```python
scale = min(box_w / img_w, box_h / img_h)          # fit, keep aspect
scaled_w, scaled_h = round(img_w * scale), round(img_h * scale)
off_x = left / center / right within the slack      # horizontal alignment
off_y = (box_h - scaled_h) // 2                      # always vertically centered
```

`render_letterboxed(image, p, background)` (in `sixel.py`) is the literal application: make a
`box_w × box_h` background canvas, paste `image.resize((scaled_w, scaled_h))` at `(off_x, off_y)`. The box
is always the full content box (not shrink-wrapped to the image), which is what keeps the frame-cache key
valid as bitmaps of different shapes flow through the same widget.

### 4.4 The inverse (hit-test) transform

```
 mouse widget-cell ──(× cell size)──▶ box px ──(− offset, ÷ scale)──▶ image px ──(first_hit vs rects)──▶ region
```

`footprint(p, cell_x, cell_y, cell_w, cell_h)` maps a cell **back** to image px. Importantly it maps the
cell's *entire rectangle* (all four corners), not just its center:

```python
left   = (cell_x * cell_w       - off_x) / scale
top    = (cell_y * cell_h       - off_y) / scale
right  = ((cell_x + 1) * cell_w - off_x) / scale
bottom = ((cell_y + 1) * cell_h - off_y) / scale
```

Then `first_hit(footprint_rect, region_rects)` returns the first region overlapping that rectangle.
Mapping the whole footprint (rather than a point) means a sub-cell target — a tiny axis label on a chart,
say — is still selectable at one-cell mouse resolution. "First overlap wins" is the disambiguation rule
when a cell straddles two regions.

### 4.5 The invariant that makes it correct

The draw path (`GraphicsImage._job_for`) and the hit-test path (`GraphicsImage._region_at`) both call the
**same** `placement(...)` with the **same** inputs (bitmap size, `content_size × cell size`, `halign`).
Because the forward transform and its inverse are derived from one shared `Placement`, the pixels you draw
and the boxes you hit-test against **cannot drift apart**. This is the entire reason `geometry.py` exists
as a separate, PIL-free, Textual-free module: compute the transform in exactly one place, use it for both
directions.

### 4.6 The scroll/zoom extension frame (prototypes, not yet in the library)

The scroll/zoom prototypes (`pdf_scroll_view.py`, `pdf_scroll_select.py`) add one more space: a
**canvas-absolute cell** frame for a zoomed canvas larger than the viewport. There, `scroll_offset`
selects which window of the big canvas crops into the viewport, selection lives in image px (invariant),
overlay tiles are encoded in canvas-absolute cells (so a tile encoded once is valid at every scroll
offset), and the blit offset is `canvas_cell − scroll_offset + centering_slack`. This is documented in
those files and `BREADCRUMBS.md`; it isn't in the shipped `widget.py` yet. Flagged here so the frame list
is complete.

---

## 5. Sixel, in lurid detail

Everything above is protocol-neutral. This section is what a **sixel** literally is, and why its one
defining limitation shapes the design of the whole library.

### 5.1 What a sixel *is*: six pixels in one printable byte

"Sixel" = "**six** pixels". It's a scheme — invented by DEC for its dot-matrix printers and the VT240/330/
340 terminals — for shipping a bitmap through a plain text byte stream. The core idea:

**One sixel character encodes a vertical column of 6 pixels.** Take a value 0–63 (six bits). Add 63. You
get a byte in the printable ASCII range `?` (63) … `~` (126). Each of the six bits corresponds to one
pixel in a vertical strip, **bit 0 = topmost pixel, bit 5 = bottommost**. A set bit paints that pixel in
the *current color*; a clear bit leaves it.

```
 value (6 bits)   add 63   char    column drawn (■ = bit set)
 0b000001  = 1      64       '@'     ■ . . . . .   (top pixel only)
 0b100000  = 32     95       '_'     . . . . . ■   (bottom pixel only)
 0b111111  = 63    126       '~'     ■ ■ ■ ■ ■ ■   (full 6-px column)
 0b000000  = 0      63       '?'     . . . . . .   (empty column)
```

So a sixel is fundamentally a **6-pixel-tall band** that you fill **left to right**, one character = one
pixel-wide column. When the band is full you drop to the next 6-pixel band and keep going. A 1280×720
image is `1280` columns wide and `720/6 = 120` bands tall.

### 5.2 The control characters inside the data

Within the sixel data stream, a handful of non-data characters do the structural work:

| Char | Name | Effect |
|------|------|--------|
| `#Pc` | color select | switch the current color to palette register `Pc` |
| `#Pc;Pu;Px;Py;Pz` | color define | define register `Pc`; `Pu=2` → RGB with `Px,Py,Pz` in **0–100** (percent, *not* 0–255); `Pu=1` → HLS |
| `$` | graphics CR | return to the **left margin of the current band** (to over-paint another color in the same band) |
| `-` | graphics NL | move **down to the next 6-pixel band** |
| `!Nc` | repeat (RLE) | repeat sixel char `c` exactly `N` times — the main compression lever |

Color is **paletted**: you define a palette, then for each band you select a color, emit that color's
columns, `$` back to the start, select the next color, emit its columns, and so on; `-` to the next band.
This per-band-per-color structure is why fewer palette colors → smaller output, and why anti-aliased text
(hundreds of subtly different colors) produces big blobs.

### 5.3 The wrapper: a DCS string

The whole payload is a **DCS — Device Control String**:

```
ESC P  [P1;P2;P3]  q   <raster attrs>  <palette defs>  <sixel data>   ESC \
└DCS┘               │                                                  └ST┘
                    └ 'q' = "this DCS is sixel"
```

- `ESC P` (0x1B 0x50) opens the DCS; `ESC \` (0x1B 0x5C, "ST" = String Terminator) closes it.
- The `q` selects sixel mode. Optional numeric params precede it: notably **`P2 = 1` means "a 0-bit
  leaves the pixel transparent"** (untouched); `P2 = 0` (the default) means a 0-bit is painted with the
  background color.

A minimal hand-written example — a 2-px-wide, 6-px-tall solid red block:

```
\x1bP q  #0;2;100;0;0  #0  ~~  -  \x1b\
└DCS┘    └ define reg 0 = RGB(100%,0,0) ┘ └ '~~' = two full 6-px columns ┘
         └ select reg 0 ┘                 └ '-' end the band ┘
```

That is the entirety of the format. A real `img2sixel` blob is the same shape with a 256-entry palette and
heavy `!Nc` run-length compression.

### 5.4 The one limitation that dictates everything: **pixels have no z-order**

A sixel paints pixels straight onto the terminal's raster. Those pixels have **no relationship to the cell
grid's compositing order**. There is no transparency-to-what's-below, no z-index, no clip rectangle. The
only rule is brutally simple: **whatever is drawn into those screen cells later wins; whatever was drawn
earlier is covered.**

Almost every non-obvious design decision in this library is a direct consequence of that one fact:

- **Hover overlays are blitted *after* the page, in the same paint.** We can't float a transparent
  highlight "above" the image. So `compose` emits the page sixel, then the overlay sixel right after it,
  so the terminal draws the overlay on top. The overlay is itself an opaque crop of the page region with a
  border drawn on it (`encode_highlight`) — not a transparent layer.

- **Transparency is effectively unavailable through `img2sixel`.** It won't emit `P2=1`: a
  transparent-interior border and a black-filled one encode to identical bytes, and 0-bits paint black.
  That's *why* the overlay is the crop-and-redraw trick rather than a hollow transparent rectangle drawn
  over the page. (Revival paths if ever needed: hand-roll a border-only sixel with `P2=1`, or add a real
  libsixel Python binding for RGBA→transparent encoding.)

- **A floating Textual widget over the image is a fight, and who wins is geometry-dependent.** A toast or
  tooltip drawn over the blob either buries the blob or gets buried — nondeterministically, depending on
  whether the float happens to cover the one "carrier cell" the escapes ride on. Because geometry-
  dependent nondeterminism is the worst possible API behavior, the widget instead **suppresses its blob
  whenever anything is in front of it**: under another screen (`screen.is_active`) or under a same-screen
  float (`first_occluder` walks the compositor map for any higher-paint-order widget overlapping us). The
  image hides deterministically and snaps back from the frame cache when the float leaves.

- **You can't partially draw a sixel** (no clip, no negative cursor origin). So a scrolled view can't clip
  one big blob to the viewport — instead each scroll offset re-encodes a *viewport-sized window* cropped
  from the zoomed canvas. Cost is viewport-bound, not canvas-bound.

- **A sixel that reaches the terminal's last row makes the terminal scroll on re-emit.** Re-emitting a
  blob that fills to the bottom line scrolls everything up one row. Owners must **reserve a bottom row**
  (a 1-cell footer) so the blob never touches the last line.

### 5.5 Surviving Textual's compositor (the strip-cut problem)

This is Textual-integration plumbing rather than the sixel protocol proper, but it's required to blit
correctly inside Textual, so it's worth knowing the shape of it (full detail in `sixel.py` and
`BREADCRUMBS.md`):

Textual paints widgets as horizontal **strips** and **divides** a widget's strips wherever another widget
(e.g. a scrollbar) overlaps its region. `Strip.divide` silently drops a strip that doesn't reach the cut,
hands zero-width segments sitting *at* an interior cut to the neighbouring chunk, and keeps trailing
control sequences only at the *final* cut. Naively-emitted sixel escapes get eaten by this the moment a
scrollbar appears.

`blob_strip(blobs, width, fill, pad)` is the one strip shape that survives. The essential tricks:

- Every escape sequence is marked as a **control segment** (`control=...`, zero cell width). A plain
  segment's escape text would be *measured as printable cells*, pushing later content past a cut where
  `Strip.divide` drops it.
- **Save/restore the cursor** (`\x1b7` / `\x1b8`) around the blobs, rather than parking it absolutely, so
  whatever paints after still lands correctly.
- The strip spans exactly the content `width`, with the escapes strictly *inside* the kept chunk and one
  real padding cell after them (that pad cell overwrites the blob's bottom-right corner; pass `pad` styled
  to match so the punch is invisible).

`blob_strip` makes blobs survive **cuts** (images coexisting with scrollbars/chrome). It does *not* solve
widgets painted *in front* (§5.4) — that's the occlusion-suppression job.

### 5.6 The encode pipeline in this codebase

Concretely, `_encode_sixel(image, options, background)` in `sixel.py`:

1. Optionally downscale (`SixelOptions.scale`) — shrinks the blob ~quadratically, but also shrinks the
   on-screen image, since sixels draw at native pixels.
2. Save the Pillow image as **PPM** (uncompressed) into a memory buffer — trivially cheap to pipe.
3. Shell out: `img2sixel -d none -E size -p <colors>`, feeding the PPM on stdin, capturing the sixel on
   stdout (decoded `latin-1`).
   - `-d none` — **no dithering**; keeps anti-aliased text crisp (dithering scatters text into speckle).
   - `-E size` — optimize the encode for **smaller output**.
   - `-p 256` — palette size. Only ≤16 meaningfully shrinks the blob, and that mangles anti-aliased text,
     so 256 is the default.

Why a subprocess and not a binding: there's no libsixel Python binding installed, and `img2sixel` is the
CLI. It (and Pillow's resize, and PyMuPDF's rasterize) **release the GIL**, so the encode runs cleanly on
a worker thread — which is what lets `GraphicsImage` keep the event loop live during the heavy encode.
Measured: libsixel `img2sixel -d none -E size` is ≈ **2.7× faster** than `textual_image`'s pure-Python
encoder (~250 ms vs ~700 ms for a full-screen 6.4 MP page), pixel-exact, ~25% bigger blob.

---

## 6. What it takes, distilled

The maintainer's question — *what is actually required to sixelize/blit in an arbitrary sixel-protocol
terminal environment?* — answered as a checklist. Each item links back to the section that explains it.

1. **A way to talk back-and-forth with the terminal at startup** (§1). Raw-mode stdin, write-query/
   read-reply-with-timeout, done *before* the UI framework grabs stdin. No timeout, no robustness; no
   "before", no answers.

2. **A sixel-capable terminal** (§2). Verified by the DA1 query (`ESC[c` → look for `4`). No reply / no
   `4` → no sixel; degrade or refuse, don't guess.

3. **An encoder** (§5.6). Here: the `img2sixel` binary. Detected as a hard prerequisite alongside terminal
   support — terminal-capable + encoder-present is the real "available" condition.

4. **The cell size in pixels — robustly** (§3). The one number bridging Textual's cell world and sixel's
   pixel world. Prefer the emulator's own XTWINOPS reply (`CSI 16t`, or `14t`/`18t`) over the kernel
   `TIOCGWINSZ` ioctl, because the ioctl can be non-zero-but-fake over SSH ("sensible garbage"). Pin the
   good answer before anything caches the bad one.

5. **The widget's absolute screen position** (§4.1, frame D). Sixels place by absolute cursor position, so
   you need the widget's `visible_region`, not just its local strip.

6. **One shared cell↔pixel transform for both draw and hit-test** (§4.5). Derive the forward map and its
   inverse from a single `Placement` so rendered pixels and hit-tested regions can't drift.

7. **A bottom-row reservation** (§5.4). So re-emitting a full-height blob doesn't scroll the terminal.

8. **An occlusion policy** (§5.4). Because sixel pixels have no z-order, suppress the blob whenever
   anything (another screen, a same-screen float) is in front of it, and repaint from cache when it
   leaves.

9. **Compositor-cut-proof emission** (§5.5). Inside Textual specifically: control-marked escapes,
   save/restore cursor, the exact `blob_strip` strip shape — or scrollbars silently eat your image.

Items 1–6 are what it takes to get a sixel onto *any* sixel terminal correctly. Items 7–9 are what it
takes to make it behave inside a live, scrollable, multi-widget Textual app without flicker, drift, or
disappearance.
