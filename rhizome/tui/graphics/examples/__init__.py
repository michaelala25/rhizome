"""Runnable examples for ``rhizome.tui.graphics``.

Small, self-contained programs that exercise the library end to end. They are *consumers* — they bring
their own content source (e.g. PyMuPDF rasterizing a PDF) and feed bitmaps into the content-dumb graphics
layer — so the library core stays ignorant of PDFs, images, etc.

Run one as a module, e.g.:  uv run python -m rhizome.tui.graphics.examples.pdf_viewer
"""
