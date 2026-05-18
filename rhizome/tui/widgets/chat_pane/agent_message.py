"""AgentMessageViewModel + view — a single contiguous run of agent output.

The VM holds an ordered list of interleaved segments (``ChatSegment`` for streamed text, ``ToolListSegment``
for tool calls) and a ``streaming`` flag that flips on ``close()``. ChatPane peek-tail routing dispatches
each incoming chunk/tool-call into the trailing AgentMessage VM, creating a new one when the tail isn't one.

Both segment streams are append-only — new segments only get appended, ``ChatSegment.body`` only grows,
``ToolListSegment.tools`` only grows. The view diffs against its last-rendered state on each ``dirty`` and
writes the deltas (using ``MarkdownStream`` for incremental text rendering). That keeps the VM → view
channel minimal: a single ``dirty`` signal carries the whole communication.
"""

from __future__ import annotations

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

    def __init__(self, vm: AgentMessageViewModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        # Mounted children in segment order — indices line up with vm.segments.
        self._segment_widgets: list[MarkdownChatMessage | ToolCallList] = []
        # Per-segment last-rendered counter: char count for ChatSegments, tool count for ToolListSegments.
        self._rendered_size: list[int] = []
        self._streams: dict[int, MarkdownStream] = {}
        self._thinking: ThinkingIndicator | None = None

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

        # Catch up the so-far body (anything that accumulated on the VM before the stream was ready) through
        # the same stream API we'll use for subsequent deltas. Keeps the rendering path uniform.
        seg = self._vm.segments[idx]
        if isinstance(seg, ChatSegment) and seg.body:
            widget._body = seg.body
            import asyncio
            asyncio.create_task(stream.write(seg.body))
            self._rendered_size[idx] = len(seg.body)


    def _update_segment(self, idx: int, seg: Segment) -> None:
        widget = self._segment_widgets[idx]
        rendered = self._rendered_size[idx]

        if isinstance(seg, ChatSegment):
            assert isinstance(widget, MarkdownChatMessage)
            if len(seg.body) == rendered:
                return

            stream = self._streams.get(idx)
            if stream is None:
                # Stream not open yet — defer. _open_stream will catch up the entire so-far body in one
                # write. Mixing inner_markdown.update here with the later stream writes corrupts append
                # bookkeeping.
                return

            delta = seg.body[rendered:]
            widget._body = seg.body
            import asyncio
            asyncio.create_task(stream.write(delta))
            self._rendered_size[idx] = len(seg.body)

        else:
            assert isinstance(widget, ToolCallList)

            for i in range(rendered, len(seg.tools)):
                name, args = seg.tools[i]
                widget.add_tool(name, args)
            self._rendered_size[idx] = len(seg.tools)


    def _mount_before_thinking(self, widget) -> None:
        if self._thinking is not None and self._thinking.is_mounted:
            self.mount(widget, before=self._thinking)
        else:
            self.mount(widget)


    def _sync_thinking(self) -> None:
        if self._vm.streaming and self._thinking is None:
            self._thinking = ThinkingIndicator()
            self.mount(self._thinking)

        elif not self._vm.streaming and self._thinking is not None:
            self._thinking.remove()
            self._thinking = None


    def on_unmount(self) -> None:
        super().on_unmount()
        for stream in self._streams.values():
            try:
                import asyncio
                asyncio.ensure_future(stream.stop())
            except Exception:
                pass
        self._streams.clear()
