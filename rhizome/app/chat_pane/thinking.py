"""ThinkingIndicator — sentinel VM + braille-spinner view.

The VM carries no state; it exists so the indicator can live in the chat feed alongside other
``FeedItem``s and be addressed by id (mount / unmount / repin to the tail). The view drives its own
animation on a Textual interval.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.app.vm import ViewModelBase


class ThinkingIndicatorVM(ViewModelBase):
    """Sentinel VM with no mutable state. The view subscribes to nothing — its animation is
    self-driven."""


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_RATE = 0.1
