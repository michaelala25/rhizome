"""AgentMessageModel + view — a single contiguous agent text segment.

The VM holds an append-only ``body`` string and a ``streaming`` flag that flips on ``close()``.
Chunks land via ``append_token``; the view's drain task pulls characters from ``body`` on a fixed
tick and writes adaptive-sized slices into a ``MarkdownStream`` so bursty arrivals paint smoothly
rather than blitting.

Unlike the previous design, this VM represents *one* segment — not a whole agent turn. Tool calls
live in their own ``ToolMessageModel`` entries in the feed; alternating chat segments and tool
lists are routed (in step 2) by the chat-pane VM or (in step 3) by the ``AgentStreamRouter``. The
thinking indicator is likewise its own feed entry, not a child of this view.
"""

from __future__ import annotations

import asyncio
import math

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from rhizome.tui.types import Mode

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.tui.widgets.shared.markdown_math import (
    MarkdownMathStream, MarkdownWithMath, agent_body_widget, open_stream)
from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view


@register_feed_view(AgentMessageModel)
class AgentMessage(ViewBase[AgentMessageModel]):
    """Renders an ``AgentMessageModel`` with adaptive markdown streaming.

    The drain task wakes on every VM event (``_wakeup``) and writes adaptive-sized slices into the
    ``MarkdownStream`` on a fixed tick. While the VM is open, slices are sized so pending content
    would drain in roughly ``_CATCHUP_BUDGET_MS``; once closed, the snappier ``_TAIL_BUDGET_MS``
    kicks in so the segment doesn't linger. The drain exits exactly once after the VM has closed
    *and* the rendered length matches ``body``.
    """

    DEFAULT_CSS = f"""
    AgentMessage {{
        padding: 1 2 0 2;
        height: auto;
        layout: vertical;
    }}
    AgentMessage.learn-mode {{
        border: round rgb(60, 80, 160);
        margin: 0 2;
    }}
    AgentMessage.review-mode {{
        border: round rgb(120, 60, 160);
        margin: 0 2;
    }}
    AgentMessage.--commit-selectable {{
        border: round rgb(140, 120, 50);
    }}
    AgentMessage.--commit-selectable.--commit-cursor {{
        border: round rgb(220, 190, 60);
    }}
    AgentMessage.--commit-selected {{
        border: round rgb(60, 160, 80);
    }}
    AgentMessage.--commit-selected.--commit-cursor {{
        border: round rgb(80, 200, 100);
    }}
    AgentMessage .msg-header {{
        height: auto;
        width: 1fr;
    }}
    AgentMessage .msg-prefix {{
        height: auto;
    }}
    AgentMessage .commit-checkbox {{
        height: auto;
        width: auto;
        margin-right: 1;
        display: none;
    }}
    AgentMessage.--commit-selectable .commit-checkbox,
    AgentMessage.--commit-selected .commit-checkbox {{
        display: block;
    }}
    AgentMessage .agent-body {{
        width: 1fr;
        color: rgb(204, 204, 204);
    }}
    """

    _TICK_MS = 40
    _CATCHUP_BUDGET_MS = 500
    _TAIL_BUDGET_MS = 200
    _MIN_SLICE_CHARS = 2

    def __init__(self, vm: AgentMessageModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._markdown: Markdown | MarkdownWithMath | None = None
        self._stream: MarkdownStream | MarkdownMathStream | None = None
        self._rendered: int = 0
        self._drain_task: asyncio.Task[None] | None = None
        self._wakeup: asyncio.Event = asyncio.Event()

        if vm.mode == Mode.LEARN:
            self.add_class("learn-mode")
        elif vm.mode == Mode.REVIEW:
            self.add_class("review-mode")

    def compose(self) -> ComposeResult:
        prefix = f"[bold rgb(200, 100, 200)]agent:[/bold rgb(200, 100, 200)] "
        with Horizontal(classes="msg-header"):
            yield Static("□", classes="commit-checkbox")
            yield Static(prefix, classes="msg-prefix")
        # MarkdownWithMath when a graphics backend + LaTeX are present, else a plain Markdown (same surface).
        yield agent_body_widget("", classes="agent-body")

    def on_mount(self) -> None:
        self._markdown = self.query_one(".agent-body", Widget)
        if self._vm.streaming:
            # Live segment at mount: open the MarkdownStream after Textual finishes mounting
            # the inner Markdown, then wire up the drain task. Tokens arriving via
            # ``append_token`` dirty the VM, which pokes ``_wakeup`` and drives ``_drain_loop``.
            self.call_after_refresh(self._open_stream)
            self._refresh()
        else:
            # Sealed at mount — typically a remount from branch navigation. Paint the full body
            # in one shot rather than routing it through the drain pipeline. This fixes two
            # bugs: (1) when the VM was cancelled mid-stream, the drain loop's cancelled check
            # short-circuits before any tick runs, leaving the message blank on revisit; and
            # (2) the one-tick lag that made branch swaps feel sluggish even for fully-rendered
            # bodies. We keep ``_stream`` as ``None`` here since a sealed VM accepts no further
            # tokens, so the stream API is never needed.
            self._markdown.update(self._vm.body)
            self._rendered = len(self._vm.body)
            self._apply_commit_decoration()

    def _apply_commit_decoration(self) -> None:
        self.set_class(self._vm.is_selectable and not self._vm.is_selected, "--commit-selectable")
        self.set_class(self._vm.is_selected, "--commit-selected")
        self.set_class(self._vm.is_cursor, "--commit-cursor")
        try:
            checkbox = self.query_one(".commit-checkbox", Static)
        except Exception:
            return
        checkbox.update("■" if self._vm.is_selected else "□")
        if self._vm.is_cursor:
            self.scroll_visible()

    def _open_stream(self) -> None:
        if self._markdown is None:
            return
        try:
            self._stream = open_stream(self._markdown)
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
        self._apply_commit_decoration()

    async def _drain_loop(self) -> None:
        try:
            while True:
                if self._vm.streaming and self._rendered >= len(self._vm.body):
                    await self._wakeup.wait()
                self._wakeup.clear()

                # User-cancelled mid-stream: stop painting immediately. ``mark_cancelled`` emits
                # dirty (which sets ``_wakeup``) so a sleeping loop wakes up here, sees the flag,
                # and exits without draining the remaining buffer. The partial rendered slice
                # stays as-is — the chat-pane appends a "(user cancelled)" system message right
                # below it so the cut-off is intentional-looking, not orphaned.
                if self._vm.cancelled:
                    return

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

        # First write to this widget instance: dump whatever body exists in one shot. For a
        # freshly-constructed VM this is typically the first token or two, so the visual difference
        # vs. the slicing path is negligible. The case this really fixes is *remount* — cursor
        # navigation brings a previously-displayed AgentMessageModel back into view with its
        # full body already populated, and without this branch the catch-up logic would slow-stream
        # the existing content back in like a fake re-stream. New tokens that arrive after this
        # initial dump (i.e. the VM is still streaming) fall through to the budgeted-slice path
        # below and animate at the usual cadence.
        if self._rendered == 0:
            await self._stream.write(self._vm.body)
            self._rendered = len(self._vm.body)
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
