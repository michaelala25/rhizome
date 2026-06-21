"""Word-range selection: the ``SelectionModel`` mixin and the fit-whole ``SelectableImage``.

Selection state is a drag-built range of *word indices* (image px, so it survives zoom and scroll), with
the selected words merged into one rect per text line (per ``block``/``line``) and washed with a uniform
``Fill`` tint — a uniform wash makes overlapping line bars compose for free.

``SelectionModel`` is the pure, viewport-independent half: the anchor/focus state, run-merging, the drag
mouse handlers, and ``SelectionChanged``. It delegates the two viewport-specific pieces to its host —
``_word_at`` (the cell→word hit-test) and the tile rendering — so both ``SelectableImage`` (fit-whole) and
``ScrollSelectableImage`` (scrolling) reuse it. The host also brings ``Throttle`` (``_request_repaint``)
and defines its own ``SelectionChanged`` message (so handler names stay per-widget).
"""

from typing import NamedTuple

from textual import events
from textual.message import Message

from rhizome.tui.graphics.render.backend import Fill
from rhizome.tui.graphics.render.overlays import ImageWithOverlays


class Word(NamedTuple):
    """One selectable word: its image-px ``rect``, ``text``, and the ``block``/``line`` that group runs."""
    rect: tuple
    text: str
    block: int
    line: int


class SelectionModel:
    """Mixin: a drag-built word-range selection in word indices (image-px, viewport-independent).

    The host must populate ``self._words`` (in ``show``), call ``_init_selection`` once, define a
    ``SelectionChanged`` ``Message`` and ``_word_at(cell_x, cell_y)`` (the viewport→word hit-test), and
    provide ``_request_repaint`` (the ``Throttle`` mixin). The selection state machine, run-merging, and
    emit live here; the drag handlers replace any hover/click behaviour the host's base widget had.
    """

    def _init_selection(self) -> None:
        self._anchor: int | None = None
        self._focus: int | None = None
        self._dragging = False

    @property
    def selected_text(self) -> str:
        return " ".join(self._words[i].text for i in self._selected_indices())

    def clear(self) -> None:
        self._anchor = self._focus = None
        self._request_repaint()
        self._emit()

    # -- model -----------------------------------------------------------------------------------

    def _selected_indices(self) -> range:
        if self._anchor is None or self._focus is None:
            return range(0)
        lo, hi = sorted((self._anchor, self._focus))
        return range(lo, hi + 1)

    def _selection_runs(self) -> list[tuple]:
        """Merge the selected words into one rect per text line — continuous bars, not per-word boxes."""
        runs: dict[tuple, list] = {}
        for i in self._selected_indices():
            word = self._words[i]
            run = runs.get((word.block, word.line))
            if run is None:
                runs[(word.block, word.line)] = list(word.rect)
            else:
                run[0], run[1] = min(run[0], word.rect[0]), min(run[1], word.rect[1])
                run[2], run[3] = max(run[2], word.rect[2]), max(run[3], word.rect[3])
        return [tuple(run) for run in runs.values()]

    def _emit(self) -> None:
        indices = list(self._selected_indices())
        text = " ".join(self._words[i].text for i in indices)
        self.post_message(self.SelectionChanged(text, len(indices)))

    def _word_at(self, cell_x: int, cell_y: int) -> int | None:
        raise NotImplementedError                            # the host's viewport→word hit-test

    # -- drag (replaces the base widget's hover/click) -------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.capture_mouse()                                  # keep getting moves even if the drag leaves us
        self._dragging = True
        self._anchor = self._focus = self._word_at(event.offset.x, event.offset.y)
        self._request_repaint()
        self._emit()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        idx = self._word_at(event.offset.x, event.offset.y)   # moves over whitespace -> selection holds
        if idx is not None and idx != self._focus:
            if self._anchor is None:                          # drag began on whitespace: anchor on first word
                self._anchor = idx
            self._focus = idx
            self._request_repaint()
            self._emit()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False
        self.capture_mouse(False)

    def on_click(self, event: events.Click) -> None:
        """Suppress any inherited click behaviour — a click is just a one-word selection via down/up."""

    def on_leave(self, event: events.Leave) -> None:
        """Suppress any inherited hover-leave — selection is driven by the drag handlers."""


class SelectableImage(SelectionModel, ImageWithOverlays):
    """A fit-whole page with a drag-built word selection, washed with a uniform tint."""

    class SelectionChanged(Message):
        """The selection changed — ``text`` is the selected words joined by spaces, ``count`` how many."""

        def __init__(self, text: str, count: int) -> None:
            super().__init__()
            self.text = text
            self.count = count

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._init_selection()
        self._words: list[Word] = []
        self._overlay_style = Fill()                          # selection tint instead of the hover outline

    def show(self, source, words=()) -> None:
        """Display ``source`` (bitmap or ``ImageSource``) with its selectable ``Word`` list."""
        self._words = list(words)
        self._anchor = self._focus = None
        super().show(source, [(w.rect, w.text) for w in self._words])   # regions drive the inherited hit-test

    def _word_at(self, cell_x: int, cell_y: int) -> int | None:
        return self._region_at(cell_x, cell_y)                # the inherited placement hit-test

    def _overlays_for(self, job, frame) -> list:
        tiles = [self._overlay_tile(frame, run) for run in self._selection_runs()]
        return [tile for tile in tiles if tile is not None]
