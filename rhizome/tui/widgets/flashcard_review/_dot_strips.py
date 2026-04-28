"""Dot-strip widget for the FlashcardReview view — one glyph per card with a
scrollable window when the card list is wider than the available content width.
"""

from __future__ import annotations

from textual import events
from textual.widgets import Static

from .flashcard import Flashcard


# Dot-strip colors. Cursor uses a different glyph (◉) so it reads even when the
# card under it is also colored (e.g. blue while pending auto-score).
_DOT_UNSCORED = "rgb(220,220,220)"
_DOT_DONE = "rgb(85,85,85)"
_DOT_PENDING = "rgb(120,160,230)"
_DOT_PENDING_APPROVAL = "rgb(235,180,90)"
_DOT_FAILED = "rgb(235,100,100)"
_DOT_CHEVRON = "rgb(110,110,110)"
# Per-state glyphs. Shape encodes lifecycle (unfinished / requeued / done /
# skipped); color encodes attention state (pending auto-score, awaiting
# approval, failed). The cursor glyph overrides per-state shape so the
# cursor is always visible regardless of color.
_DOT_UNSCORED_GLYPH = "○"  # U+25CB WHITE CIRCLE — FRONT, REVEALED_*, SCORED_PENDING_APPROVAL
_DOT_SCORED_GLYPH = "•"    # U+2022 BULLET — SCORED with HARD/GOOD/EASY
_DOT_SKIPPED_GLYPH = "–"   # U+2013 EN DASH — SCORED with SKIPPED
_DOT_AWAITING_GLYPH = "◴"  # U+25F4 WHITE CIRCLE WITH UPPER LEFT QUADRANT — AWAITING_REVEAL
_DOT_CURSOR_GLYPH = "◉"    # U+25C9 FISHEYE — overrides per-state shape


class _DotStrip(Static):
    """Bottom-of-widget progress strip — one dot per card.

    Color encodes per-card status (unscored / done / pending auto-score /
    auto-score failed). The card under the cursor gets a different glyph
    so it stays visible regardless of color.

    When the card list is wider than the available content width, the
    strip scrolls to keep the cursor visible and shows ``<`` / ``>``
    chevrons on the truncated side(s).
    """

    def __init__(self, max_dots=10, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._cards: list[Flashcard] = []
        self._cursor: int = 0
        self._scroll: int = 0
        self._max_dots = max_dots

    def update_state(self, cards: list[Flashcard], cursor: int) -> None:
        self._cards = cards
        self._cursor = cursor
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw()

    def _redraw(self) -> None:
        n = len(self._cards)
        if n == 0:
            self.update("")
            return

        # Each dot is glyph + space (2 chars). Reserve 2 chars on each
        # side for chevrons even when not shown, so dots don't shift
        # horizontally as scroll state changes.
        width = self.size.width or 0
        if width <= 0:
            # Pre-mount / pre-layout — fall back to the cap; resize will refit.
            width_cap = self._max_dots
        else:
            width_cap = max(1, (width - 4) // 2)
        visible = min(n, self._max_dots, width_cap)

        # Reconcile scroll so the cursor stays in the window.
        if self._cursor < self._scroll:
            self._scroll = self._cursor
        elif self._cursor >= self._scroll + visible:
            self._scroll = self._cursor - visible + 1
        self._scroll = max(0, min(self._scroll, n - visible))

        start = self._scroll
        end = start + visible

        def _chevron_color(off_screen: list[Flashcard]) -> str:
            # Highlight the chevron in the approval color when there's a pending-approval
            # card off-screen on that side, so the user knows to scroll for it.
            for c in off_screen:
                if c.state == Flashcard.State.SCORED_PENDING_APPROVAL:
                    return _DOT_PENDING_APPROVAL
            return _DOT_CHEVRON

        left = (
            f"[{_chevron_color(self._cards[:start])}]<[/]"
            if start > 0 else " "
        )
        right = (
            f"[{_chevron_color(self._cards[end:])}]>[/]"
            if end < n else " "
        )
        dots = " ".join(
            self._dot(self._cards[i], i == self._cursor)
            for i in range(start, end)
        )
        self.update(f"{left} {dots} {right}")

    @staticmethod
    def _dot(card: Flashcard, is_cursor: bool) -> str:
        # Glyph: cursor first, then per-state shape. SCORED splits between filled
        # (rated) and dash (skipped). Pending-auto / pending-approval reuse the
        # unscored open circle since their color already encodes the attention.
        if is_cursor:
            glyph = _DOT_CURSOR_GLYPH
        elif card.state == Flashcard.State.AWAITING_REVEAL:
            glyph = _DOT_AWAITING_GLYPH
        elif card.state == Flashcard.State.SCORED:
            glyph = (
                _DOT_SKIPPED_GLYPH if card.score == Flashcard.Score.SKIPPED
                else _DOT_SCORED_GLYPH
            )
        else:
            glyph = _DOT_UNSCORED_GLYPH

        if card.auto_scoring_failed and card.state != Flashcard.State.SCORED:
            color = _DOT_FAILED
        elif card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
            color = _DOT_PENDING
        elif card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
            # Distinct from the in-flight blue: this card is waiting on the user, not on
            # the scorer.
            color = _DOT_PENDING_APPROVAL
        elif card.state == Flashcard.State.SCORED:
            color = _DOT_DONE
        else:
            color = _DOT_UNSCORED

        # Flagged is orthogonal to state — overlay underline on whatever glyph/color
        # the per-state branches picked.
        style = f"{color} underline" if card.flagged else color
        return f"[{style}]{glyph}[/]"
