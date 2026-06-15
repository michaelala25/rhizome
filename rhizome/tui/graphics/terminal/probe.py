"""The single terminal probe: one raw excursion that asks geometry *and* capability together.

We need two things only the terminal knows — its cell size (XTWINOPS) and whether it speaks sixel (DA1).
Instead of asking in separate raw-mode excursions, we send them all at once and read until the DA1 reply,
which doubles as the "all replies are in" sentinel: terminals answer queries in the order received, and
DA1 ("what are you?") is near-universally supported, so its reply arriving means every earlier query has
been answered or silently skipped.

One excursion, one timeout, one failure mode — **no DA1 reply means the terminal isn't a responsive
graphics terminal**. `environment.initialize` runs this once, before Textual starts, and everything
downstream (backend selection, cell size) is pure interpretation of the returned `TerminalProbe`.
"""

import re
from dataclasses import dataclass, field

from rhizome.tui.graphics.terminal.query import exchange

# Sent together; DA1 (ESC[c) goes LAST so its reply terminates the batch.
#   ESC[16t -> cell size px      (reply code 6)
#   ESC[14t -> text area px      (reply code 4)
#   ESC[18t -> text area cells   (reply code 8)
#   ESC[c   -> device attributes (reply ends with 'c'; capability "4" == sixel)
_PAYLOAD = "\x1b[16t\x1b[14t\x1b[18t\x1b[c"
_XTWINOPS = re.compile(r"\x1b\[(\d+);(\d+);(\d+)t")
_DA1 = re.compile(r"\x1b\[\?([\d;]*)c")


@dataclass(frozen=True)
class TerminalProbe:
    """What the one probe learned. ``xtwinops`` maps each reply code (6/4/8) to its ``(a, b)`` pair."""

    responded: bool                                       # did the DA1 sentinel come back?
    da1: tuple[str, ...] = ()                             # DA1 capability codes, e.g. ("62", "4", "6")
    xtwinops: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def supports_sixel(self) -> bool:
        return "4" in self.da1


def probe_terminal(timeout: float = 0.5) -> TerminalProbe:
    """Run the one probe and parse it. Returns ``responded=False`` if no DA1 reply arrived."""
    reply = exchange(_PAYLOAD, end="c", timeout=timeout)
    if reply is None:
        return TerminalProbe(responded=False)
    xtwinops = {int(m[1]): (int(m[2]), int(m[3])) for m in _XTWINOPS.finditer(reply)}
    da1 = _DA1.search(reply)
    if da1 is None:                                       # geometry may still have arrived; capability didn't
        return TerminalProbe(responded=False, xtwinops=xtwinops)
    codes = tuple(code for code in da1[1].split(";") if code)
    return TerminalProbe(responded=True, da1=codes, xtwinops=xtwinops)
