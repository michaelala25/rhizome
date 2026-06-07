"""Tiny ▶/▼ collapse toggle used by the proposal widgets."""

from __future__ import annotations

from textual.message import Message
from textual.widgets import Static


class CollapseButton(Static, can_focus=False):
    """Tiny ▶/▼ toggle. Posts ``Pressed`` on click; parent flips its own collapsed flag."""

    DEFAULT_CSS = """
    CollapseButton {
        width: 3;
        height: 1;
        content-align: center middle;
        color: rgb(120,120,120);
        background: transparent;
    }
    CollapseButton:hover {
        color: white;
    }
    """

    class Pressed(Message):
        """User clicked the collapse button."""

    def on_click(self, event) -> None:
        self.post_message(CollapseButton.Pressed())
