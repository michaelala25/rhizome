"""Kitty / Terminal Graphics Protocol backend — placeholder.

TGP is cell-addressable and z-index aware, so a frame transmits differently and an overlay composites
as its own higher-z image rather than the sixel blit-on-top trick — ``encode``/``compose`` will
diverge from the sixel backend. Left unimplemented until it can be validated on a real TGP terminal;
``available`` returns False so ``select_backend`` never picks it. When implemented, ``available`` will
become ``textual_image.renderable.tgp.query_terminal_support()``.
"""

from rhizome.tui.graphics_prototype.backend import GraphicsBackend

_UNIMPLEMENTED = "the kitty/TGP backend is not implemented yet"


class KittyBackend(GraphicsBackend):
    @classmethod
    def available(cls) -> bool:
        return False

    def prepare(self, bitmap, placement, cell_size, *, background):
        raise NotImplementedError(_UNIMPLEMENTED)

    def encode(self, job):
        raise NotImplementedError(_UNIMPLEMENTED)

    def encode_highlight(self, frame, rect):
        raise NotImplementedError(_UNIMPLEMENTED)

    def compose(self, frame, highlight, crop, *, region):
        raise NotImplementedError(_UNIMPLEMENTED)
