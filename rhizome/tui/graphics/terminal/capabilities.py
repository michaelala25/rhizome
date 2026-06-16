"""The structured "why no graphics" reasons, and whether any sixel encoder is available.

The terminal facts (does it speak sixel) come from the one ``probe``; this module only holds the failure
vocabulary and the encoder check. ``environment`` maps a probe + these to a backend or a reason.
"""

import shutil
from enum import Enum


class GraphicsUnavailable(Enum):
    """Why no graphics backend could be selected — surfaced verbatim by the image widget."""

    NOT_INITIALIZED = "graphics not initialized — call graphics.initialize() before the app starts"
    NOT_A_TERMINAL = "not a terminal (no tty) — graphics need a real terminal"
    NO_PROTOCOL = "this terminal supports no graphics protocol (sixel / kitty)"
    ENCODER_MISSING = "sixel is supported but no encoder is available (install img2sixel, or numpy)"


def encoder_available() -> bool:
    """Whether *some* sixel encoder is usable: the fast ``img2sixel`` binary, or the numpy fallback.

    ``img2sixel`` is a speed optimization, not a requirement — the backend falls back to a pure-Python
    (numpy-vectorized) encoder when it's absent. Only when *neither* exists is sixel truly unavailable.
    """
    if shutil.which("img2sixel") is not None:
        return True
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False
