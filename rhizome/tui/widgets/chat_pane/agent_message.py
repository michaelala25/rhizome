"""AgentMessageViewModel + view — a single contiguous run of agent output.

The VM holds an ordered list of interleaved segments (``ChatSegment`` for streamed text, ``ToolListSegment``
for tool calls) and a ``streaming`` flag that flips on ``close()``. ChatPane peek-tail routing dispatches
each incoming chunk/tool-call into the trailing AgentMessage VM, creating a new one when the tail isn't one.

Both segment streams are append-only — new segments only get appended, ``ChatSegment.body`` only grows,
``ToolListSegment.tools`` only grows. The view diffs against its last-rendered state on each ``dirty`` and
writes deltas. Tool list deltas go straight to the widget; chat-text deltas are smoothed through a per-view
drain task that pushes adaptive slices into the ``MarkdownStream`` on a fixed tick. The drain rate adapts so
bursty arrivals catch up within a target window rather than blitting in one frame, and the thinking
indicator stays mounted until the drain catches up to ``close()``.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any

from textual.app import ComposeResult
from textual.widgets import Markdown
from textual.widgets.markdown import MarkdownStream

from rhizome.tui.types import Mode, Role

from ..message import MarkdownChatMessage
from ..thinking import ThinkingIndicator
from ..tool_call_list import ToolCallList
from ..view_base import ViewBase
from ..view_model_base import ViewModelBase


@dataclass
class ChatSegment:
    body: str = ""


@dataclass
class ToolListSegment:
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


Segment = ChatSegment | ToolListSegment


class AgentMessageViewModel(ViewModelBase):

    def __init__(self, *, mode: Mode = Mode.IDLE) -> None:
        super().__init__()
        self.segments: list[Segment] = []
        self.streaming: bool = True
        self.mode: Mode = mode

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def append_token(self, text: str) -> None:
        """Route a streamed text delta into the trailing ``ChatSegment``, opening a new one if the last
        segment isn't a chat segment.
        """
        assert self.streaming, "append_token after close()"
        if not text:
            return
        
        if not self.segments or not isinstance(self.segments[-1], ChatSegment):
            self.segments.append(ChatSegment())

        seg = self.segments[-1]
        assert isinstance(seg, ChatSegment)
        seg.body += text

        self.emit(self.dirty)


    def add_tool_call(self, name: str, args: dict[str, Any] | None = None) -> None:
        """Append a tool call to the trailing ``ToolListSegment``, opening a new one if the last segment
        isn't a tool list.
        """
        assert self.streaming, "add_tool_call after close()"

        args = args or {}
        if not self.segments or not isinstance(self.segments[-1], ToolListSegment):
            self.segments.append(ToolListSegment())

        seg = self.segments[-1]
        assert isinstance(seg, ToolListSegment)
        seg.tools.append((name, args))

        self.emit(self.dirty)


    def close(self, *, empty_fallback: str | None = None) -> None:
        """Finalize the message. Idempotent. If no segments accumulated and ``empty_fallback`` is provided,
        append a single chat segment with that body so the run still leaves a visible trace.
        """
        if not self.streaming:
            return
        if not self.segments and empty_fallback is not None:
            self.segments.append(ChatSegment(body=empty_fallback))
        self.streaming = False

        self.emit(self.dirty)


class AgentMessageView(ViewBase[AgentMessageViewModel]):
    """Renders an ``AgentMessageViewModel``: a vertical stack of segment widgets plus a trailing
    ``ThinkingIndicator`` while ``vm.streaming``.

    Diffs the VM's append-only segments against its own last-rendered state on each ``dirty`` and writes the
    deltas — using ``MarkdownStream`` for incremental text, ``ToolCallList.add_tool`` for new tool entries.
    """

    DEFAULT_CSS = """
    AgentMessageView {
        height: auto;
        layout: vertical;
    }
    """

    # Smoothing parameters for the chat-text drain. The drain wakes every ``_TICK_MS`` and writes a slice
    # sized so that current pending content would fully drain in roughly ``_CATCHUP_BUDGET_MS`` (or
    # ``_TAIL_BUDGET_MS`` once the VM has closed — slightly snappier so the message doesn't linger).
    _TICK_MS = 40
    _CATCHUP_BUDGET_MS = 500
    _TAIL_BUDGET_MS = 200
    _MIN_SLICE_CHARS = 2

    def __init__(self, vm: AgentMessageViewModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)

        # Mounted children in segment order — indices line up with vm.segments.
        self._segment_widgets: list[MarkdownChatMessage | ToolCallList] = []

        # Per-segment last-rendered counter: char count actually pushed to the MarkdownStream for
        # ChatSegments, tool count for ToolListSegments.
        self._rendered_size: list[int] = []
        self._streams: dict[int, MarkdownStream] = {}
        self._thinking: ThinkingIndicator | None = None
        self._drain_task: asyncio.Task[None] | None = None

        # Signal raised on every VM event (new chunks, close). The drain blocks on this when idle and
        # re-evaluates state when it fires — so VM lifecycle, not a polling clock, owns the drain's wakeups.
        self._wakeup: asyncio.Event = asyncio.Event()

    def compose(self) -> ComposeResult:
        # Children are mounted imperatively by _refresh as the VM grows.
        yield from ()

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # VM → view
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._reconcile_segments()
        
        # Every VM signal — new chunks or close — pokes the drain. Both transitions are events the drain
        # needs to react to (consume more, or wind down), and `_refresh` is the single place we observe
        # them, so this is the natural choke point.
        self._ensure_drain_task()
        self._wakeup.set()
        self._sync_thinking()


    def _reconcile_segments(self) -> None:
        # 1. Mount any new segments.
        for idx in range(len(self._segment_widgets), len(self._vm.segments)):
            self._mount_segment(idx)

        # 2. Stream deltas into existing segments whose content grew.
        for idx, seg in enumerate(self._vm.segments):
            self._update_segment(idx, seg)


    def _mount_segment(self, idx: int) -> None:
        seg = self._vm.segments[idx]

        if isinstance(seg, ChatSegment):
            # Always mount empty; the so-far body (if any) gets written through the stream once it opens.
            # Mixing Markdown's constructor-driven _initial_markdown path with subsequent stream appends
            # races on content that ends at a block boundary (e.g. "...:\n\n") because Markdown.append's
            # _last_parsed_line bookkeeping gets desynced against the _on_mount-side update.
            widget = MarkdownChatMessage(role=Role.AGENT, content="", mode=self._vm.mode)

            self._mount_before_thinking(widget)
            self._segment_widgets.append(widget)

            # Nothing rendered yet — _open_stream will catch up the body.
            self._rendered_size.append(0)

            # Open a stream once Textual finishes mounting the inner Markdown.
            self.call_after_refresh(self._open_stream, idx)

        else:
            widget = ToolCallList()

            self._mount_before_thinking(widget)
            self._segment_widgets.append(widget)

            # Seed any tools that were already on the segment before mount.
            for name, args in seg.tools:
                widget.add_tool(name, args)

            self._rendered_size.append(len(seg.tools))


    def _open_stream(self, idx: int) -> None:
        widget = self._segment_widgets[idx]
        if not isinstance(widget, MarkdownChatMessage):
            return

        try:
            stream = Markdown.get_stream(widget.inner_markdown)
        except Exception:
            return

        self._streams[idx] = stream

        # Any body that landed before the stream was ready will be picked up by the drain on its next wake.
        # Poke the drain in case it's currently idle waiting for the stream to become writable.
        self._wakeup.set()


    def _update_segment(self, idx: int, seg: Segment) -> None:
        if isinstance(seg, ChatSegment):
            # ChatSegment writes are routed through the drain task so bursty chunks paint at a smooth,
            # adaptive rate. We do not touch the stream here — the drain compares ``seg.body`` against
            # ``_rendered_size[idx]`` when woken.
            return

        widget = self._segment_widgets[idx]
        assert isinstance(widget, ToolCallList)

        rendered = self._rendered_size[idx]
        for i in range(rendered, len(seg.tools)):
            name, args = seg.tools[i]
            widget.add_tool(name, args)

        self._rendered_size[idx] = len(seg.tools)

    # ------------------------------------------------------------------
    # Smoothing drain
    # ------------------------------------------------------------------

    def _ensure_drain_task(self) -> None:
        # One drain per view, created the first time `_refresh` observes the VM. Lives from that moment
        # until the VM has closed *and* every segment is fully painted. Idempotent — only the first call
        # creates the task; subsequent calls are no-ops.
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_loop())


    async def _drain_loop(self) -> None:
        """Wakeup-driven drain: idles on ``_wakeup`` between VM events, paces with sleep during a burst.

        Lifecycle:
          * Created once when the view first sees the VM and lives for the message's full lifetime.
          * While the VM is open and there's nothing pending, blocks on ``_wakeup`` (set by ``_refresh``
            on every VM signal). It is *informed* of new chunks and of close, not polling for them.
          * While there's pending content, consumes one adaptive slice per ``_TICK_MS`` so bursts paint
            smoothly rather than blitting.
          * Exits exactly once after the VM has closed and every segment has fully drained — taking the
            thinking indicator down on the way out so the "done" cue lands with the final painted glyph.
        """
        try:
            while True:
                # Idle path: stream still open and nothing pending → wait until the VM signals us.
                if self._vm.streaming and self._all_caught_up():
                    await self._wakeup.wait()
                self._wakeup.clear()

                await self._drain_tick()

                if not self._vm.streaming and self._all_caught_up():
                    self._sync_thinking()
                    return

                # Pace the next slice. During a burst the wakeup may fire again mid-sleep; that's fine,
                # `_wakeup.set()` is idempotent and we'll observe it on the next iteration's clear.
                await asyncio.sleep(self._TICK_MS / 1000)
        finally:
            self._drain_task = None


    async def _drain_tick(self) -> None:
        budget_ms = self._TAIL_BUDGET_MS if not self._vm.streaming else self._CATCHUP_BUDGET_MS
        budget_ticks = max(1, budget_ms // self._TICK_MS)

        for idx, seg in enumerate(self._vm.segments):
            if not isinstance(seg, ChatSegment):
                continue

            stream = self._streams.get(idx)
            if stream is None:
                continue  # stream not opened yet — next tick

            rendered = self._rendered_size[idx]
            pending = len(seg.body) - rendered
            if pending <= 0:
                continue

            slice_size = max(self._MIN_SLICE_CHARS, math.ceil(pending / budget_ticks))
            slice_size = min(slice_size, pending)
            end = rendered + slice_size
            delta = seg.body[rendered:end]

            widget = self._segment_widgets[idx]
            assert isinstance(widget, MarkdownChatMessage)
            # TODO: ``_body`` is our own MarkdownChatMessage attribute (used for non-rendering reads like
            # copy-to-clipboard), kept in lockstep with the painted prefix. That means a mid-stream copy
            # would yield the slice we've rendered so far rather than the full canonical body the VM
            # already knows about. Worth reconciling — either drive ``_body`` from ``seg.body`` directly,
            # or have MarkdownChatMessage derive it from the underlying stream — when we revisit.
            widget._body = seg.body[:end]

            await stream.write(delta)

            self._rendered_size[idx] = end


    def _all_caught_up(self) -> bool:
        for idx, seg in enumerate(self._vm.segments):
            if not isinstance(seg, ChatSegment):
                continue

            # Stream not yet opened but body already exists → not caught up.
            if self._streams.get(idx) is None and seg.body:
                return False
            
            if len(seg.body) > self._rendered_size[idx]:
                return False
            
        return True


    def _mount_before_thinking(self, widget) -> None:
        if self._thinking is not None and self._thinking.is_mounted:
            self.mount(widget, before=self._thinking)
        else:
            self.mount(widget)


    def _sync_thinking(self) -> None:
        if self._vm.streaming and self._thinking is None:
            self._thinking = ThinkingIndicator()
            self.mount(self._thinking)

        elif not self._vm.streaming and self._thinking is not None and self._all_caught_up():
            # Hold the indicator until the drain has flushed remaining text — otherwise the "done" cue
            # lands before the message has finished painting.
            self._thinking.remove()
            self._thinking = None


    def on_unmount(self) -> None:
        super().on_unmount()

        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None

        for stream in self._streams.values():
            try:
                asyncio.ensure_future(stream.stop())
            except Exception:
                pass

        self._streams.clear()
