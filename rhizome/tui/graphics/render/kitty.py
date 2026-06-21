"""Kitty / Terminal Graphics Protocol backend — placeholder.

TGP is cell-addressable and z-index aware, so a frame transmits differently and an overlay composites
as its own higher-z image rather than the sixel blit-on-top trick — ``encode``/``compose`` will diverge
from the sixel backend. Left unimplemented until it can be validated on a real TGP terminal;
``terminal.capabilities.terminal_supports_kitty`` returns False so ``environment`` never selects it.
"""

from rhizome.tui.graphics.render.backend import GraphicsBackend

_UNIMPLEMENTED = "the kitty/TGP backend is not implemented yet"


class KittyBackend(GraphicsBackend):
    def prepare(self, bitmap, placement, cell_size, *, background):
        raise NotImplementedError(_UNIMPLEMENTED)

    def encode(self, job):
        raise NotImplementedError(_UNIMPLEMENTED)

    def encode_highlight(self, frame, rect, style):
        raise NotImplementedError(_UNIMPLEMENTED)

    def compose(self, frame, overlays, crop, *, region):
        raise NotImplementedError(_UNIMPLEMENTED)
