"""ThinkingIndicator — sentinel VM + braille-spinner view.

The VM carries no state; it exists so the indicator can live in the chat feed alongside other
``FeedItem``s and be addressed by id (mount / unmount / repin to the tail). The view drives its own
animation on a Textual interval.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.thinking import ThinkingIndicatorModel
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_RATE = 0.1


@register_feed_view(ThinkingIndicatorModel)
class ThinkingIndicator(ViewBase[ThinkingIndicatorModel]):
    """Braille-spinner with a 'thinking...' label."""

    DEFAULT_CSS = """
    ThinkingIndicator {
        height: 1;
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 0 0 4;
        color: $text-muted;
    }
    """

    def __init__(self, vm: ThinkingIndicatorModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._frame = 0
        self._static: Static | None = None

    def compose(self) -> ComposeResult:
        self._static = Static(f"{_FRAMES[0]} thinking...")
        yield self._static

    def on_mount(self) -> None:
        self.set_interval(_TICK_RATE, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_FRAMES)
        if self._static is not None:
            self._static.update(f"{_FRAMES[self._frame]} thinking...")
