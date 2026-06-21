"""How many pixels is one character cell — the single number that bridges cells and pixels.

Textual measures everything in cells; sixel draws in pixels. Every conversion between them multiplies by
the cell size, so getting it wrong scales *and* skews every image and every hit-test by the same factor —
and it fails silently (the picture is just tiny and misaligned, no error). This module turns the one
terminal ``probe`` (plus the free ioctl/env signals) into that figure, and keeps it current as the window
resizes.

Two phases, because stdin is only ours before Textual starts:

  SEED  (`from_probe`, run by `environment.initialize`) — interpret the one probe + ioctl + env.
  LIVE  (`update_from_pixels`, fed by resize events)    — recompute from the pixel size Textual reports,
                                                           which the emulator delivers via in-band resize.

# ========================================================================================================
# SEED PRIORITY — sources in order, and the terminal context each one is for
# ========================================================================================================
#
# 1. XTWINOPS 16t (probe reply code 6) — the emulator's own cell size, in pixels, directly.
#      For: essentially every modern graphics-capable terminal, INCLUDING over ssh. Authoritative: it
#      comes from the same program that will place the sixels, and rides the ssh channel as ordinary I/O.
#
# 2. XTWINOPS 14t/18t (reply codes 4/8) — derive cell = area_px / area_cells, for emulators with 14t/18t
#      but not 16t. Comes free from the same probe (no extra round-trip), so it sits above the ioctl.
#
# 3. ioctl TIOCGWINSZ (xpixel/cols × ypixel/rows) — the kernel pty's pixels. A free, instant syscall, but
#      its pixel fields are OPTIONAL and, over ssh, are whatever the client forwarded — the Windows
#      OpenSSH → WSL sshd path sends a HARDCODED 640x480 (a ~2x6 px cell: non-zero, so naively trusted,
#      yet nonsense). So we consult it only when NOT over ssh (`over_ssh()`), and only after XTWINOPS.
#
# 4. Environment TEXTUAL_CELL_WIDTH/HEIGHT — for web terminals (textual-serve), no escape channel.
#
# 5. NONE answered — fall back to the VT340 10x20 default, but flagged NOT CONFIDENT. It is the genre's
#      lingua franca and is often exactly right (e.g. virtual-VT340 grids), so we render with it rather
#      than refuse — but the flag lets an owner notice the guess, and the live phase can correct it.
"""

import os
from typing import NamedTuple

from rhizome.tui.graphics.terminal.query import over_ssh, tiocgwinsz


class CellSize(NamedTuple):
    """Pixels per character cell."""
    width: int
    height: int


def _from_env() -> CellSize | None:
    w, h = os.environ.get("TEXTUAL_CELL_WIDTH", ""), os.environ.get("TEXTUAL_CELL_HEIGHT", "")
    if w.isdigit() and h.isdigit() and int(w) > 0 and int(h) > 0:
        return CellSize(int(w), int(h))
    return None


def resolve(probe) -> tuple[CellSize, str, bool]:
    """Pick the cell size from a ``TerminalProbe`` (+ ioctl/env): ``(cell, source_label, confident)``."""
    xt = probe.xtwinops
    if 6 in xt and xt[6][0] > 0 and xt[6][1] > 0:          # 16t: (height, width)
        return CellSize(xt[6][1], xt[6][0]), "XTWINOPS 16t (emulator cell size)", True
    if 4 in xt and 8 in xt and xt[8][0] > 0 and xt[8][1] > 0:   # 14t area px / 18t area cells
        (ah, aw), (rows, cols) = xt[4], xt[8]
        return CellSize(aw // cols, ah // rows), "XTWINOPS 14t/18t (derived)", True
    if not over_ssh():                                     # ioctl pixels — trustworthy only off ssh
        win = tiocgwinsz()
        if win and win[0] > 0 and win[1] > 0 and win[2] > 0 and win[3] > 0:
            rows, cols, xpix, ypix = win
            return CellSize(xpix // cols, ypix // rows), "TIOCGWINSZ ioctl (kernel pty pixels)", True
    env = _from_env()
    if env is not None:
        return env, "TEXTUAL_CELL_* env (web terminal)", True
    return CellSize(10, 20), "VT340 default (no geometry reported)", False


class CellMetrics:
    """The current cell size, a human label of how it was determined, and whether we trust it.

    Seeded once pre-Textual from the probe, then updated from the pixel size Textual reports on resize.
    ``confident`` is False only for the VT340 fallthrough — see source 5 above; an owner can inspect it.
    """

    def __init__(self, cell: CellSize, source: str, confident: bool = True) -> None:
        self.current = cell
        self.source = source
        self.confident = confident

    @classmethod
    def from_probe(cls, probe) -> "CellMetrics":
        return cls(*resolve(probe))

    def update_from_pixels(self, grid_cols: int, grid_rows: int, pixel_w: int, pixel_h: int) -> bool:
        """LIVE path: recompute from a resize's window pixel size; True if the cell size changed.

        ``grid_*`` is the whole terminal in cells and ``pixel_*`` the whole terminal in pixels (from
        Textual's ``Resize.pixel_size``). Always pair the two at the *window* scale — never one widget's.
        """
        if grid_cols <= 0 or grid_rows <= 0 or pixel_w <= 0 or pixel_h <= 0:
            return False
        new = CellSize(pixel_w // grid_cols, pixel_h // grid_rows)
        if new.width <= 0 or new.height <= 0 or new == self.current:
            return False
        self.current, self.source, self.confident = new, "in-band resize (live emulator pixel size)", True
        return True
