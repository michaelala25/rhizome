"""AgentMessageViewModel + view — a single contiguous agent text segment.

The VM holds an append-only ``body`` string and a ``streaming`` flag that flips on ``close()``.
Chunks land via ``append_token``; the view's drain task pulls characters from ``body`` on a fixed
tick and writes adaptive-sized slices into a ``MarkdownStream`` so bursty arrivals paint smoothly
rather than blitting.

Unlike the previous design, this VM represents *one* segment — not a whole agent turn. Tool calls
live in their own ``ToolMessageViewModel`` entries in the feed; alternating chat segments and tool
lists are routed (in step 2) by the chat-pane VM or (in step 3) by the ``AgentStreamRouter``. The
thinking indicator is likewise its own feed entry, not a child of this view.
"""

from __future__ import annotations

import asyncio
import math

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from rhizome.tui.colors import Colors
from rhizome.tui.types import Mode

from ..view_base import ViewBase
from ..view_model_base import ViewModelBase


class AgentMessageViewModel(ViewModelBase):
    """A single contiguous run of agent text. Append-only ``body``; ``streaming`` flips on close."""

    def __init__(self, *, mode: Mode = Mode.IDLE) -> None:
        super().__init__()
        self.body: str = ""
        self.streaming: bool = True
        self.mode: Mode = mode

    @property
    def is_empty(self) -> bool:
        return self.body == ""

    def append_token(self, text: str) -> None:
        assert self.streaming, "append_token after close()"
        if not text:
            return
        self.body += text
        self.emit(self.dirty)

    def close(self) -> None:
        if not self.streaming:
            return
        self.streaming = False
        self.emit(self.dirty)


class AgentMessageView(ViewBase[AgentMessageViewModel]):
    """Renders an ``AgentMessageViewModel`` with adaptive markdown streaming.

    The drain task wakes on every VM event (``_wakeup``) and writes adaptive-sized slices into the
    ``MarkdownStream`` on a fixed tick. While the VM is open, slices are sized so pending content
    would drain in roughly ``_CATCHUP_BUDGET_MS``; once closed, the snappier ``_TAIL_BUDGET_MS``
    kicks in so the segment doesn't linger. The drain exits exactly once after the VM has closed
    *and* the rendered length matches ``body``.
    """

    DEFAULT_CSS = f"""
    AgentMessageView {{
        padding: 1 2 0 2;
        height: auto;
        layout: vertical;
    }}
    AgentMessageView.learn-mode {{
        border: round {Colors.LEARN_AGENT_BORDER};
        margin: 0 2;
    }}
    AgentMessageView.review-mode {{
        border: round {Colors.REVIEW_AGENT_BORDER};
        margin: 0 2;
    }}
    AgentMessageView .msg-header {{
        height: auto;
        width: 1fr;
    }}
    AgentMessageView .msg-prefix {{
        height: auto;
    }}
    AgentMessageView .agent-body {{
        width: 1fr;
        color: rgb(204, 204, 204);
    }}
    """

    _TICK_MS = 40
    _CATCHUP_BUDGET_MS = 500
    _TAIL_BUDGET_MS = 200
    _MIN_SLICE_CHARS = 2

    def __init__(self, vm: AgentMessageViewModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._markdown: Markdown | None = None
        self._stream: MarkdownStream | None = None
        self._rendered: int = 0
        self._drain_task: asyncio.Task[None] | None = None
        self._wakeup: asyncio.Event = asyncio.Event()

        if vm.mode == Mode.LEARN:
            self.add_class("learn-mode")
        elif vm.mode == Mode.REVIEW:
            self.add_class("review-mode")

    def compose(self) -> ComposeResult:
        prefix = f"[bold {Colors.AGENT_PREFIX}]agent:[/bold {Colors.AGENT_PREFIX}] "
        with Horizontal(classes="msg-header"):
            yield Static(prefix, classes="msg-prefix")
        yield Markdown("", classes="agent-body")

    def on_mount(self) -> None:
        self._markdown = self.query_one(".agent-body", Markdown)
        # The MarkdownStream isn't writable until Textual finishes mounting the inner Markdown —
        # mirror the call_after_refresh dance from the legacy AgentMessage view.
        self.call_after_refresh(self._open_stream)
        self._refresh()

    def _open_stream(self) -> None:
        if self._markdown is None:
            return
        try:
            self._stream = Markdown.get_stream(self._markdown)
        except Exception:
            return
        # Any body that arrived before the stream was ready gets picked up on the next drain tick.
        self._wakeup.set()

    def _refresh(self) -> None:
        # Every VM event (new chunk or close) needs to poke the drain — that's the single place
        # we observe both transitions.
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_loop())
        self._wakeup.set()

    async def _drain_loop(self) -> None:
        try:
            while True:
                if self._vm.streaming and self._rendered >= len(self._vm.body):
                    await self._wakeup.wait()
                self._wakeup.clear()

                await self._drain_tick()

                if not self._vm.streaming and self._rendered >= len(self._vm.body):
                    return

                await asyncio.sleep(self._TICK_MS / 1000)
        finally:
            self._drain_task = None

    async def _drain_tick(self) -> None:
        if self._stream is None:
            return
        pending = len(self._vm.body) - self._rendered
        if pending <= 0:
            return

        budget_ms = self._TAIL_BUDGET_MS if not self._vm.streaming else self._CATCHUP_BUDGET_MS
        budget_ticks = max(1, budget_ms // self._TICK_MS)
        slice_size = max(self._MIN_SLICE_CHARS, math.ceil(pending / budget_ticks))
        slice_size = min(slice_size, pending)
        end = self._rendered + slice_size
        delta = self._vm.body[self._rendered:end]

        await self._stream.write(delta)
        self._rendered = end

    def on_unmount(self) -> None:
        super().on_unmount()
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None
        if self._stream is not None:
            try:
                asyncio.ensure_future(self._stream.stop())
            except Exception:
                pass
            self._stream = None
