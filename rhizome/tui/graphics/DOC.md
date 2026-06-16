# Terminal Graphics: How It Actually Works

> **For future agents:** This is a hand-written explanatory document, authored *for the human maintainer*
> as a from-first-principles reference on the terminal mechanics this library rests on. It is not a
> generated `CONTEXT.md` and it is not auto-maintained. **Do not edit, "freshen", restructure, or
> regenerate this file unless the maintainer explicitly asks you to.** If the code drifts from what is
> written here, surface the discrepancy in conversation rather than silently rewriting the prose. The
> at-a-glance, agent-facing summary of this directory lives in `CONTEXT.md`; keep that one current
> instead.

---

This document explains the low-level terminal magic underneath `rhizome/tui/graphics`. The goal is that
after reading it you can answer, for any new terminal/SSH/tmux situation: *what does it actually take to
put pixels on this screen and know where they landed?* Everything is grounded in the real code in this
directory, but the bulk of the text is the "why", because the "why" is where all the sharp edges live.

The four things it covers, in order:

1. **The substrate** — how a program asks a terminal *anything* (the query/reply dance). Both backend
   detection and cell-size detection are built on this, so it comes first.
2. **Backend detection** — how we decide whether this terminal can do sixel, kitty/TGP, or nothing.
3. **Cell size** — how we learn how many *pixels* a character cell is, across local / SSH / tmux, and
   how we keep it correct as the window resizes. This is the linchpin of the whole library.
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

This library owns that trick end to end, in `terminal/query.py`. Here is the dance in full, because both
§2 and §3 depend on it working *exactly right*.

### 1.1 Why it needs "raw mode"

Normally a terminal runs in **cooked / canonical mode**: stdin is line-buffered (the program sees nothing
until Enter is pressed) and echoed (typed characters are shown). That is perfect for typing commands and
useless for reading a machine reply, which arrives as a burst of control characters with no newline.

So to read a reply you must temporarily switch the terminal into **raw** (or **cbreak**) mode: characters
become available to the program immediately, one at a time, with no echo and no line buffering. After the
read you must restore the original mode, or you'll wreck the user's shell.

In this codebase that switch lives in `terminal/query.py`: `exchange` puts stdin into cbreak with
`tty.setcbreak(stdin)` for the duration of one round-trip and restores the saved `termios` state in a
`finally`. It is the *only* function in the library that ever does this (see §1.4 and §1.5). The
diagnostic `probe_geometry.py` (in the frozen prototypes) does the same thing by hand with `tty.setraw()`
so you can watch the bytes.

### 1.2 The dance

```
1. put stdin into raw/cbreak mode
2. write the query escape sequence to stdout, and FLUSH
3. read stdin byte-by-byte until you see the reply's terminator,
   OR until a short timeout elapses with no data
4. restore the original terminal mode
```

Step 3's **timeout is not optional**. If the terminal does *not* support the query, it sends nothing at
all — there is no "unsupported" reply. Without a timeout you would block forever. The timeout is the only
signal that means "this terminal can't answer that". This is why `exchange` reads with
`select([fd], [], [], timeout)` and, on silence, returns whatever it has accumulated so far (possibly
nothing) rather than raising — and why, throughout this library, "no reply" is treated as "feature
absent", never as an error.

`exchange(payload, *, end, timeout)` accumulates stdin until the buffer ends with `end` (the terminator of
the last reply it expects) or the terminal goes quiet, then restores the mode and returns the buffer.

### 1.5 We make exactly one excursion

Rather than call `exchange` once per question, `terminal/probe.py` batches *all* the startup queries into
a single payload and reads them back in one go. It sends the cell-size queries (`16t`/`14t`/`18t`) **and**
the capability query (DA1) together, and reads until the DA1 reply — which terminals always send last,
because they answer queries in the order received. DA1 ("what are you?") is near-universally supported, so
its reply is a reliable "all earlier replies are in (or were silently skipped)" sentinel.

The payoff is exactly what minimizes the §1.3 hazards and the startup cost: **one** raw-mode excursion,
**one** timeout, and **one** failure mode — *no DA1 reply means the terminal isn't a responsive graphics
terminal*. Everything downstream (backend selection in §2, cell size in §3) is then pure interpretation of
the returned `TerminalProbe`, doing no further terminal I/O.

### 1.3 The two flakiness hazards (real, accepted)

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
query/reply — backend detection *and* cell-size seeding — **must happen at startup, before the Textual
app takes over stdin.** After that, the channel is gone.

This is why:

- `graphics.initialize()` (in `environment.py`) is the single startup entry point that runs the one
  terminal probe (§1.5) and derives *both* backend selection and the cell-size seed from it. **The
  consumer must call it before `App.run()`.** There are no import-time side effects: importing the package
  touches the terminal not at all; `initialize()` is the one moment it does.
- The cell size is seeded **once** into the process-global environment (`CellMetrics.from_probe(...)`). It
  cannot rely on a later, in-widget lazy query, because by then Textual's reader owns stdin and the reply
  would never arrive. After startup the cell size instead stays correct by riding Textual's *resize
  events*, which carry the pixel size without us having to ask (§3.5).

> Rule of thumb: **anything that needs to hear the terminal talk back must run in
> `graphics.initialize()`, pre-app.** A widget cannot query the terminal — the answer has to be captured
> at boot and read from the environment thereafter.

---

## 2. Determining the available backend (sixel / kitty / none)

"Is this terminal capable of graphics protocol X?" is answered from the one probe (§1.5). The DA1 query is
part of that batch; `TerminalProbe.supports_sixel` (in `terminal/probe.py`) interprets its reply.

### 2.1 Sixel: the Primary Device Attributes (DA1) query

The probe sends, as the last query in its batch:

```
ESC [ c          (written as "\x1b[c")
```

This is **DA1 — Primary Device Attributes** — the ancient "what are you?" query every VT-style terminal
answers. The reply looks like:

```
ESC [ ? 62 ; 4 ; 6 ; 9 ; 22 c
```

The numbers between `?` and `c` are a semicolon-separated list of capability codes. **Code `4` means
"sixel graphics".** So the entire detection is: the probe reads until `c`, splits the DA1 list on `;`, and
`supports_sixel` checks whether `"4"` is in it. DA1 doubles as the batch's "all replies in" sentinel
(§1.5), so this one query both detects sixel *and* marks the end of the cell-size replies.

### 2.2 Kitty/TGP: send a tiny image, expect "OK"

A TGP-capable terminal is detected by transmitting a 1-pixel TGP image with a query flag and waiting for
the protocol's `…;OK` acknowledgement (timeout → not supported). This isn't wired up yet: `render/kitty.py`
is a stub, so `_select` never offers it, until it can be validated on a real TGP terminal.

### 2.3 Selection maps capabilities to a backend *or a reason*

Terminal support is necessary but not sufficient — we also need *an* encoder. Selection happens in
`environment._select(probe)`, pure interpretation of the probe (the `not is_terminal()` gate already ran
in `initialize`, so it isn't repeated here):

| Situation | Result |
|---|---|
| `probe.supports_sixel` **and** an encoder is available | `SixelBackend` |
| `probe.supports_sixel` but no encoder at all | `GraphicsUnavailable.ENCODER_MISSING` |
| not sixel-capable (no reply, or replied without `4`) | `GraphicsUnavailable.NO_PROTOCOL` |

"An encoder is available" (`encoder_available()`) means **`img2sixel` on PATH _or_ numpy importable**:
`img2sixel` is a ~2.7× speed optimization, but when it's absent the backend falls back to a numpy-
vectorized pure-Python encoder (§5.6). So `ENCODER_MISSING` is now a genuinely rare last resort — it fires
only when *neither* exists — rather than a hard dependency on the binary.

### 2.4 Selection policy: best available, or *nothing* (with a reason)

`graphics.initialize()` runs the selection and stores the result — a backend, or a `GraphicsUnavailable`
reason — in the process-global environment. The deliberate design choice worth flagging: **there is no
half-cell / Unicode-block fallback.** If no true graphics backend is available, the environment holds no
backend, and `Image` renders the structured reason as its notice ("…the img2sixel encoder is
missing…", etc.). We would rather show a precise explanation than degrade to chunky block-character
approximations. A fallback renderer, if ever wanted, belongs as another `GraphicsBackend` slotted into
`_select()` — not as a special case bolted onto the widget.

---

## 3. Cell size: the one pair of numbers everything hinges on

To draw or hit-test anything we must know **how many pixels wide and tall a single character cell is**.
Textual gives us geometry in cells; sixel needs pixels; cell size is the multiplier between them. This
section is the longest because obtaining it correctly is genuinely hard, and getting it wrong fails
*silently* — the image just renders tiny and misaligned with no error.

We own the whole story in `terminal/cellsize.py`. It has two phases, because stdin is only ours before
Textual starts:

- **Seed** (`resolve(probe)`, run by `initialize()`, pre-Textual) — interpret the one probe's XTWINOPS
  replies, plus the free ioctl/env signals.
- **Live** (`CellMetrics.update_from_pixels`, fed by resize events) — once Textual runs and we can no
  longer query, we ingest the pixel size the emulator reports on every resize.

Let's take the seed sources one at a time, then the live phase.

### 3.1 Source — `TIOCGWINSZ` (the kernel ioctl)

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

`terminal/query.py`'s `tiocgwinsz()` does `fcntl.ioctl(fd, TIOCGWINSZ, buf)` and unpacks
`(rows, cols, xpixel, ypixel)`. From it, `resolve()` computes `cell_w = xpixel / cols`,
`cell_h = ypixel / rows`.

**The crucial question: who fills in `ws_xpixel` / `ws_ypixel`?** The *terminal emulator* does, when it
creates the pty and on every resize. And here's the rub:

- `ws_row` / `ws_col` are essentially always correct — every terminal maintains them.
- `ws_xpixel` / `ws_ypixel` are **optional**, and many terminals never set them. When unset they read as
  **zero**. Zero is *honest*: we skip the source and fall through. Fine.
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

A ~2×6 px cell. Nonsense — but **non-zero**. A naive cascade that consults the ioctl *first* and trusts
any non-zero result would confidently compute this ~2×6 cell, and then every image is tiny and every
cell↔pixel transform skewed by the same factor — no exception, no warning. This is the "sensible garbage"
problem: **a value plausible enough to be trusted and wrong enough to break everything.** It is the exact
reason our seed priority tries the emulator's own escape report *before* the ioctl (§3.4) — and, belt and
suspenders, skips the ioctl's pixel fields entirely when `over_ssh()` is true (an upfront, env-based
signal: `$SSH_CONNECTION`/`$SSH_TTY`).

### 3.3 Source — XTWINOPS escape queries (the SSH-proof source)

The fix is to ask the source that actually knows and is reachable even over SSH: **the terminal emulator
itself**, via escape-sequence queries. These belong to the **XTWINOPS** family — xterm's "window
operations", the `CSI Ps ; Ps ; Ps t` sequences. (Sometimes called "xwinops" — same thing.) The relevant
query codes:

| Send (stdout) | Meaning of the query        | Reply (stdin)            | Gives us                |
|---------------|-----------------------------|--------------------------|-------------------------|
| `CSI 14 t`    | text-area size in **pixels**| `CSI 4 ; H ; W t`        | total area px           |
| `CSI 16 t`    | **cell size** in **pixels** | `CSI 6 ; H ; W t`        | px per cell *directly*  |
| `CSI 18 t`    | text-area size in **chars** | `CSI 8 ; H ; W t`        | rows / cols             |

(`CSI` = `ESC [`. Note the reply codes are the query code minus 10: 14→4, 16→6, 18→8.) All three ride the
single probe (§1.5), so `resolve(probe)` reads them out of `probe.xtwinops` for free: it uses `16t`
directly, and otherwise derives `cell = (area px from 14t) / (area chars from 18t)`.

**Why these are reliable over SSH, when `TIOCGWINSZ` isn't.** The query and its reply are *ordinary
terminal I/O*. `CSI 16 t` is bytes the program writes to stdout; the emulator's reply is bytes that flow
back over stdin. Over SSH that round trip rides the SSH data channel like any other input/output — **the
kernel pty's `winsize` and SSH's window-size signaling are entirely out of the loop.** The answer comes
straight from the program that will actually draw the pixels, so it is authoritative *by construction*:
even if the number is a "virtual" geometry (see §3.6), it's the same geometry the emulator will use when
it places our sixels. **Placement only needs internal consistency, and asking the renderer directly is
how you guarantee it.**

### 3.4 Our seed priority: emulator-first, ioctl-off-ssh, and an honest fallback

`resolve(probe)` tries these sources in this exact order, stopping at the first that answers:

```
1. XTWINOPS 16t       — emulator cell size, directly (probe reply code 6). Authoritative; works over SSH.
2. XTWINOPS 14t+18t   — derived (area px ÷ area cells; codes 4 and 8), for emulators with these but no 16t.
3. ioctl, OFF SSH ONLY — kernel pty pixels (xpixel/cols × ypixel/rows). Free syscall; skipped over ssh.
4. env vars           — TEXTUAL_CELL_WIDTH / TEXTUAL_CELL_HEIGHT, for web terminals (textual-serve).
5. VT340 default      — 10 × 20 px, the last resort — flagged NOT CONFIDENT (§3.6).
```

The non-obvious choice is **both XTWINOPS sources ahead of the ioctl**. The "instinctive" order would put
the ioctl first (it's a cheap local syscall; the escape query is a round-trip) — but the XTWINOPS replies
now ride the single probe (§1.5), so they cost *nothing extra* to prefer, and preferring them is the clean
structural fix for the §3.2 trap: on the fake-640×480 SSH path the emulator *does* answer `16t`, so we
never reach the lying ioctl value. We avoid the trap by **ordering**, not by second-guessing the ioctl's
number (which we can't — 640×480 is indistinguishable from a real value). As a second guard, source 3 is
**gated behind `over_ssh()`** — over ssh we don't trust the ioctl's pixel fields at all, even if XTWINOPS
somehow failed; we would rather fall to the (flagged) VT340 default than render at a confidently-wrong
~2×6 cell.

**The fallback is honest about being a guess.** When *no* source answers (source 5), `resolve` still
returns a usable `CellSize` — 10×20 — but marks `CellMetrics.confident = False`. We deliberately do *not*
turn this into a hard "unavailable": 10×20 is the genre's lingua franca and is frequently exactly right
(it's literally what virtual-VT340 grids report), and the live phase (§3.5) can correct it the moment the
window resizes. But the flag is there so an owner that wants to be strict can refuse, or warn, on a
low-confidence geometry. (This case is narrow anyway — it only arises when a terminal answers DA1+sixel
yet reports no geometry *and* gives no ioctl pixels; in every "no backend" case the cell size is never
used to render.) `metrics.source` records the winning source as a human string, so the resolution is
inspectable at runtime — useful when diagnosing a "why is the image tiny" report.

Two timing facts make the seed work (both from §1.4): it runs **pre-Textual** (the probe needs raw stdin),
and it runs **once** at `initialize()` time, stored in the singleton — nothing queries the terminal again.

### 3.5 The live phase: staying correct across resize

The seed is a single snapshot. But the cell size can *change while the app runs* — specifically on a font
zoom or DPI change (a plain window drag-resize does **not** change it; you just get more or fewer cells of
the same size). We can no longer query the terminal mid-run, so instead we ingest what Textual already
hands us.

**Textual reports the window's pixel size on resize.** On a terminal that supports **in-band window resize
(mode 2048)**, Textual's driver negotiates it automatically (`\x1b[?2048$p` to ask, `\x1b[?2048h` to
enable) and populates `Resize.pixel_size` on every resize event. We consume it via `graphics.note_resize`,
wired from the app's own `on_resize`:

```python
def on_resize(self, event: events.Resize) -> None:
    graphics.note_resize(self.size.width, self.size.height, event.pixel_size)
```

`note_resize` recomputes `cell = pixel_size ÷ grid_cells` and updates the singleton (so the next paint
picks it up — a changed cell size is a natural frame-cache miss). **Pair the pixel size with the *window*
cell grid** (`self.size` at the app level), never one widget's size — `pixel_size` is the whole window.

The honest matrix of what this achieves:

- **Terminal reports `pixel_size`** (mode-2048 capable): fully live and correct, no query needed. ✅
- **No `pixel_size`, window resize**: cell px is invariant → the seeded value still holds, and the box is
  recomputed from the new cell grid every paint anyway. Already correct. ✅
- **No `pixel_size`, font zoom, virtual-grid SSH client**: cell px is *zoom-invariant* there (`16t` stays
  10×20 across zooms), so the seed stays correct. ✅
- **No `pixel_size`, font zoom, local physical-px terminal**: genuinely unsolvable mid-session — we can't
  re-query, and a grid reflow is ambiguous between a window resize and a font zoom. ❌ This is one small,
  nameable gap, and it is exactly the gap mode-2048 closes; the rest is covered.

### 3.6 The VT340 fallback, virtual pixels, and why SSH looks soft

If every source fails, we assume **VT340** geometry: `10 × 20` px/cell. The VT340 is the historical DEC
sixel terminal; its geometry is the genre's lingua franca.

A curious thing observed over the Windows-Terminal-via-SSH path: the *correct* `CSI 16 t` answer there is
**also exactly 10×20**. Windows Terminal presents a **virtual VT340 pixel grid** and scales sixels to its
real font cell. So the seeded answer is "right" (consistent — placement and hit-testing line up perfectly,
because `16t` comes from the same program that blits), but it **caps effective resolution at 10×20 px/cell**
over that client. That's why pages look softer over that SSH path than locally: not a bug, a property of
the client's virtual grid, and not fixable server-side.

---

## 4. Coordinate frames and the transforms between them

With cell size in hand, the geometry is just arithmetic — but there are several coordinate spaces and it's
easy to lose track of which one a given number lives in. Here they all are. The pure math lives in
`render/geometry.py`; the widget (`render/image.py`) and the sixel backend (`render/sixel.py`) apply it.

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
entire pipeline is `cell_w × cell_h` (read from `cell_metrics().current`). That is precisely why §3
matters so much: one wrong cell size scales *and* skews every frame B/C transform by the same factor,
which is the exact signature of the SSH bug.

### 4.3 The forward (draw) transform

```
 image px ──(× scale, + (off_x, off_y))──▶ box/canvas px ──(encode)──▶ sixel ──(place at screen region)──▶ pixels
            └────────── Placement ───────┘
```

`placement(img_w, img_h, box_w, box_h, halign)` computes the whole forward map once and returns it as a
`Placement`:

```python
scale = min(box_w / img_w, box_h / img_h)          # fit, keep aspect
scaled_w, scaled_h = round(img_w * scale), round(img_h * scale)
off_x = left / center / right within the slack      # horizontal alignment
off_y = (box_h - scaled_h) // 2                      # always vertically centered
```

`render_letterboxed(image, p, background)` (in `render/sixel.py`) is the literal application: make a
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

The draw path (`Image._job_for`) and the hit-test path (`ImageWithOverlays._region_at`) both call the
**same** `placement(...)` with the **same** inputs (bitmap size, `content_size × cell size`, `halign`).
Because the forward transform and its inverse are derived from one shared `Placement`, the pixels you draw
and the boxes you hit-test against **cannot drift apart**. This is the entire reason `geometry.py` exists
as a separate, PIL-free, Textual-free module: compute the transform in exactly one place, use it for both
directions.

### 4.6 The scroll/zoom extension frame (prototypes, not yet in the library)

The scroll/zoom prototypes (in `rhizome/tui/graphics_prototype`-era scratch files) add one more space: a
**canvas-absolute cell** frame for a zoomed canvas larger than the viewport. There, `scroll_offset`
selects which window of the big canvas crops into the viewport, selection lives in image px (invariant),
overlay tiles are encoded in canvas-absolute cells (so a tile encoded once is valid at every scroll
offset), and the blit offset is `canvas_cell − scroll_offset + centering_slack`. It isn't in the shipped
widget yet. Flagged here so the frame list is complete.

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
correctly inside Textual, so it's worth knowing the shape of it (full detail in `render/sixel.py`):

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
widgets painted *in front* (§5.4) — that's the occlusion-suppression job (`first_occluder`).

### 5.6 The encode pipeline in this codebase

`_encode_sixel(image, options, background)` in `render/sixel.py` first applies `SixelOptions.scale` (a
downscale that shrinks the blob ~quadratically but also shrinks the on-screen image, since sixels draw at
native pixels), then dispatches to one of two encoders — **`img2sixel` is preferred when present, but it
is not required**:

- **`img2sixel` (fast path).** Save the Pillow image as **PPM** (uncompressed, cheap to pipe) and shell
  out: `img2sixel -d none -E size -p <colors>`.
   - `-d none` — **no dithering**; keeps anti-aliased text crisp (dithering scatters text into speckle).
   - `-E size` — optimize the encode for **smaller output**.
   - `-p 256` — palette size. Only ≤16 meaningfully shrinks the blob, and that mangles anti-aliased text.
  A subprocess (not a binding), so it — like Pillow's resize and PyMuPDF's rasterize — **releases the
  GIL** and runs cleanly on a worker thread, keeping the event loop live during the heavy encode.

- **`_encode_sixel_py` (pure-Python fallback).** When `img2sixel` isn't on PATH, we encode the sixel
  ourselves: Pillow quantizes to a ≤256-color palette (`"P"` mode → indexed pixels), and a numpy-
  vectorized loop emits the DCS — define the used color registers, then for each 6-row band, for each
  color present, emit that color's run-length-encoded columns (`$` carriage-returns to overprint the next
  color in the band, `-` drops to the next band). It produces larger output and is slower than
  `img2sixel`, but it is correct and self-contained (Pillow's quantize does the heavy lifting off the
  GIL). This is what makes `img2sixel` a *speed* dependency rather than a hard one.

Measured: `img2sixel -d none -E size` is ≈ **2.7× faster** than the pure-Python path (~250 ms vs ~700 ms
for a full-screen 6.4 MP page), with a ~25% larger blob — which is why it's preferred when available.

### 5.7 Future experiment: cropping an encoded sixel for faster scrolling (NOT IMPLEMENTED)

`ScrollImage` re-encodes a viewport-window from canvas pixels on every new scroll offset — ~34 ms for a
~1 MP window. (Measured: the cost is linear in pixels; the subprocess wrapper is only ~5 % of it; and
img2sixel's `-E`/`-p` knobs don't move it, so there's no free win in the flags, and chafa's sixel output
is ~3× slower.) Because the natural workflow is **zoom once to a legible size, then scroll a lot**,
scrolling is by far the more frequent action — so a way to make *scrolling* cheaper, even at the cost of a
pricier *zoom*, is worth recording.

The idea: a sixel can be **cropped directly in its encoded form**, far cheaper than re-encoding, because
the crop reuses the existing palette and the already-RLE'd runs — it skips the expensive parts of an
encode (reading pixels, re-quantizing the colour space). A C crop of pre-encoded bytes is plausibly
~2–5 ms vs ~34 ms. Sketch of the algorithm:

1. **Bands (vertical, 6 px each):** split the body on `-`; keep the bands whose rows fall within the
   crop's `[y0, y1)`. *Caveat:* crop edges are cell rows (e.g. 20 px) that don't land on the 6 px band
   grid, so snapping to the nearest band shifts content by `y0 mod 6` (0–5 px) — a faint vertical shimmer
   while scrolling cell-by-cell, unless you do the harder **band-repack** (bit-shift the 6-bit columns
   across the band boundary; the same operation §5.4's "no partial draw" rules out doing for free).
2. **Passes (horizontal, per colour):** within each kept band, split on `$`; for each colour pass walk
   its run stream and RLE-aware-slice to `[x0, x1)` (split the runs straddling the edges), keeping the
   `#n` select. Dropped leading columns naturally re-base the kept ones to x = 0.
3. **Reassemble:** rejoin passes with `$`, bands with `-`; **keep interior empty bands as a bare `-`**
   (dropping one shifts everything below up 6 px); **hoist all colour *definitions* into the header** (a
   colour first used in a dropped band would lose its `#n;2;r;g;b`); update the raster attributes.

The catch is not the crop but **what you crop from** — `ScrollImage` keeps no whole-canvas sixel, it
encodes windows. So this needs one of:

- **Encode the whole zoomed canvas once per zoom + a band index**, then crop windows from it. The per-crop
  cost is tiny, but the one-time encode grows with zoom (~17 ms at zoom 1 → ~1 s and ~10 MB held at
  zoom 8): the lag *moves to zoom time, worst exactly where scrolling hurts most*. Bound it by capping
  max zoom and shrinking the frame cache.
- **Incremental from the neighbour window:** a 1-cell scroll overlaps the cached window ~95 %, so crop the
  overlap and encode only the one new cell-row (~1 ms). Avoids the whole-canvas blow-up, but assembling
  the shifted blob *is* the 20 px-vs-6 px band-repack — at ~1 MP that must be in C (pure-Python
  byte-twiddling at that scale runs in the hundreds of ms; cf. the 411 ms Python encode).

Cheaper alternative that covers most of the benefit: **directional prefetch** — pre-encode the next
window(s) in the current scroll direction on the idle worker, so sustained panning hits the cache (cached
windows are already instant). Prefetch wins for sustained reading scrolls (~20 lines, no C); the crop wins
for **random-access** scrolling and for driving the per-step cost toward zero. Try prefetch first.

---

## 6. What it takes, distilled

The maintainer's question — *what is actually required to sixelize/blit in an arbitrary sixel-protocol
terminal environment?* — answered as a checklist. Each item links back to the section that explains it.

1. **A way to talk back-and-forth with the terminal at startup** (§1). Raw-mode stdin, write-queries/
   read-replies-with-timeout, done *before* the UI framework grabs stdin (`graphics.initialize()`), and
   **batched into a single excursion** with DA1 as the sentinel (§1.5). No timeout, no robustness; no
   "before", no answers.

2. **A sixel-capable terminal** (§2). Verified by the DA1 query (`ESC[c` → look for `4`), which rides the
   same probe. No reply / no `4` → no sixel; we record a structured `GraphicsUnavailable` reason, not a
   guess.

3. **An encoder** (§5.6) — but a *soft* one. `img2sixel` is preferred for speed; absent it, a numpy
   pure-Python encoder takes over, so a sixel terminal renders either way. Only when *neither* `img2sixel`
   nor numpy exists is sixel truly unavailable (`ENCODER_MISSING`).

4. **The cell size in pixels — robustly, and kept live** (§3). The one number bridging Textual's cell
   world and sixel's pixel world. Seed it from the emulator's own XTWINOPS reply *before* the kernel ioctl
   (and skip the ioctl entirely over ssh, where it can be non-zero-but-fake — "sensible garbage"); fall
   back to a *flagged-low-confidence* VT340 default rather than a hard failure; then keep it current from
   Textual's `Resize.pixel_size` as the window/font changes.

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
