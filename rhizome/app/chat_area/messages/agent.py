"""AgentMessageModel + view — a single contiguous agent text segment.

The VM holds an append-only ``body`` string and a ``streaming`` flag that flips on ``close()``.
Chunks land via ``append_token``; the view's drain task pulls characters from ``body`` on a fixed
tick and writes adaptive-sized slices into a ``MarkdownStream`` so bursty arrivals paint smoothly
rather than blitting.

This VM represents *one* segment — not a whole agent turn. Tool calls
live in their own ``ToolMessageModel`` entries in the feed; alternating chat segments and tool
lists are routed (in step 2) by the chat-area VM or (in step 3) by the ``ChatAreaStreamRouter``. The
thinking indicator is likewise its own feed entry, not a child of this view.
"""

from __future__ import annotations

import asyncio
import math

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from rhizome.tui.types import Mode

from rhizome.app.model import ViewModelBase


class AgentMessageModel(ViewModelBase):
    """A single contiguous run of agent text — an answer segment, or (when ``thinking``) an
    adaptive-thinking summary. Append-only ``body``; ``streaming`` flips on close."""

    def __init__(self, *, mode: Mode = Mode.IDLE, thinking: bool = False) -> None:
        super().__init__()
        self.body: str = ""
        self.streaming: bool = True
        self.cancelled: bool = False
        self.mode: Mode = mode

        # A thinking segment carries an adaptive-thinking summary rather than the answer. The view
        # renders it dimmed and borderless; commit selection excludes it (not a committable answer).
        self.thinking: bool = thinking

    @property
    def is_empty(self) -> bool:
        return self.body == ""

    def append_token(self, text: str) -> None:
        assert self.streaming, "append_token after close()"
        if not text:
            return
        self.body += text
        self.emit(self.Callbacks.OnDirty)

    def close(self) -> None:
        if not self.streaming:
            return
        self.streaming = False
        self.emit(self.Callbacks.OnDirty)

    def mark_cancelled(self) -> None:
        """Signal the view's drain loop to exit immediately instead of catching up.

        The drain loop normally pulls budget-sized slices off ``body`` until rendered catches up,
        even after ``close()`` flips ``streaming`` to False. When the user cancels mid-stream we
        don't want the buffered text to keep painting — flipping this flag and emitting dirty
        (which sets the view's wakeup event) causes the loop to short-circuit on its next
        iteration and exit, leaving the partially-rendered slice frozen as-is.

        Idempotent. Doesn't touch ``body`` or ``streaming`` — callers typically follow with
        ``close()`` so the segment is also formally sealed.
        """
        if self.cancelled:
            return
        self.cancelled = True
        self.emit(self.Callbacks.OnDirty)
