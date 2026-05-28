"""ResourceList — read-only widget for browsing resources."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rhizome.db import Resource
from rhizome.tui.dock import DockableWidgetMixin
from rhizome.tui.types import Arrangement
from rhizome.tui.widgets.legacy.resource.view_model import ResourceListViewModel

_DIM = "rgb(100,100,100)"
_HINT = "rgb(80,80,80)"
_ACCENT = "rgb(255,80,80)"
_FOCUS_GREEN = "rgb(100,200,100)"
_ALT_GREY = "rgb(180,180,180)"
_ID_COLOR = "rgb(80,80,80)"


class ResourceList(Widget, DockableWidgetMixin, can_focus=True):
    """Read-only resource list with detail panel for browsing Resource objects."""

    show_ids: reactive[bool] = reactive(False)

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "dismiss", show=False),
    ]

    DEFAULT_CSS = """
    ResourceList {
        height: auto;
        layout: vertical;
        padding: 0 1;
    }
    ResourceList #rl-list-scroll {
        height: auto;
        max-height: 10;
        margin: 1 0 1 0;
    }
    ResourceList #rl-list {
        height: auto;
    }
    ResourceList #rl-detail-panel {
        border: solid $surface-lighten-2;
        padding: 1 2;
        height: auto;
    }
    ResourceList #rl-name {
        text-style: bold;
        margin-bottom: 0;
    }
    ResourceList #rl-meta {
        color: rgb(100,100,100);
        margin: 0 0 1 0;
    }
    ResourceList #rl-summary-scroll {
        height: auto;
        max-height: 10;
    }
    ResourceList #rl-summary {
        height: auto;
    }
    ResourceList #rl-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 1;
    }

    /* -- Vertical arrangement -- */
    ResourceList.--arrange-vertical #rl-list-scroll {
        max-height: 70%;
        overflow-y: auto;
    }
    ResourceList.--arrange-vertical #rl-detail-panel {
        max-height: 25;
        layout: vertical;
    }
    ResourceList.--arrange-vertical #rl-summary-scroll {
        height: 1fr;
        max-height: 100%;
    }
    """

    class CursorChanged(Message):
        """Posted when the cursor moves to a different resource."""

        def __init__(self, resource: Resource | None) -> None:
            super().__init__()
            self.resource = resource

    cursor: reactive[int] = reactive(0)

    def __init__(self, view_model: ResourceListViewModel | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vm = view_model or ResourceListViewModel()

    # -- Properties that read/write through to the view model -------------

    @property
    def _resources(self) -> list[Resource]:
        return self._vm.resources

    @_resources.setter
    def _resources(self, value: list[Resource]) -> None:
        self._vm.resources = value

    def compose(self) -> ComposeResult:
        yield Static("", id="rl-empty")
        with VerticalScroll(id="rl-list-scroll"):
            yield Static(id="rl-list")
        with Vertical(id="rl-detail-panel"):
            yield Static(id="rl-name")
            yield Static(id="rl-meta")
            with VerticalScroll(id="rl-summary-scroll"):
                yield Static(id="rl-summary")

    def on_mount(self) -> None:
        self.show_ids = self._vm.show_ids
        self.cursor = self._vm.cursor
        self.set_class(self.dock_arrangement == Arrangement.VERTICAL, "--arrange-vertical")
        self._apply_empty_state()
        if self._resources:
            self._render_list()
            self._render_detail()
            self._scroll_cursor_visible()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_resources(self, resources: list[Resource]) -> None:
        """Replace the displayed resources and reset the cursor."""
        self._resources = list(resources)
        self.cursor = 0
        self._apply_empty_state()
        if self._resources:
            self._render_list()
            self._render_detail()
            self._scroll_cursor_visible()
            self.post_message(self.CursorChanged(self._resources[0]))
        else:
            self.post_message(self.CursorChanged(None))

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_show_ids(self) -> None:
        self._vm.show_ids = self.show_ids
        if self._resources:
            self._render_list()

    def watch_cursor(self) -> None:
        self._vm.cursor = self.cursor
        if self._resources:
            self._render_list()
            self._render_detail()
            self._scroll_cursor_visible()
            resource = self._resources[min(self.cursor, len(self._resources) - 1)]
            self.post_message(self.CursorChanged(resource))
        else:
            self.post_message(self.CursorChanged(None))

    def _scroll_cursor_visible(self) -> None:
        self.call_after_refresh(self._do_scroll_cursor_visible)

    def _do_scroll_cursor_visible(self) -> None:
        scroll = self.query_one("#rl-list-scroll", VerticalScroll)
        if scroll.size.height == 0:
            return
        line_height = 1
        cursor_top = self.cursor * line_height
        cursor_bottom = cursor_top + line_height
        if cursor_top < scroll.scroll_y:
            scroll.scroll_y = cursor_top
        elif cursor_bottom > scroll.scroll_y + scroll.size.height:
            scroll.scroll_y = cursor_bottom - scroll.size.height

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _apply_empty_state(self) -> None:
        empty = not self._resources
        self.query_one("#rl-empty", Static).display = empty
        self.query_one("#rl-list-scroll", VerticalScroll).display = not empty
        self.query_one("#rl-detail-panel", Vertical).display = not empty
        if empty:
            self.query_one("#rl-empty", Static).update("(No resources)")

    def _render_list(self) -> None:
        vertical = self.dock_arrangement == Arrangement.VERTICAL
        num_width = len(str(len(self._resources))) + 2

        right_parts = [
            r.loading_preference.value if r.loading_preference else "—"
            for r in self._resources
        ]
        max_right = max((len(r) for r in right_parts), default=0)

        # Compute max name width from available space.
        # Layout: marker(2) + num(num_width+1) + name + id? + gap(2) + right(max_right)
        total_width = self.size.width - 2  # padding
        id_overhead = max((len(str(r.id)) + 4 for r in self._resources), default=0) if self.show_ids else 0
        max_name_width = total_width - 2 - (num_width + 1) - id_overhead - 2 - max_right
        max_name_width = max(max_name_width, 10)  # floor

        text = Text()
        for i, resource in enumerate(self._resources):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            marker = "► " if is_selected else "  "
            num = f"{i + 1}. ".rjust(num_width + 1)

            if is_selected and self.has_focus:
                style = f"bold {_FOCUS_GREEN}"
                marker_style = f"bold {_FOCUS_GREEN}"
                right_style = _DIM
            elif is_selected:
                style = "bold"
                marker_style = "bold"
                right_style = _DIM
            else:
                style = "" if i % 2 == 0 else _ALT_GREY
                marker_style = ""
                right_style = _DIM

            name = resource.name
            if not vertical and len(name) > max_name_width:
                name = name[: max_name_width - 1] + "…"

            text.append(marker, style=marker_style)
            text.append(num, style=style)
            text.append(name, style=style)
            if self.show_ids:
                text.append(f"  [{resource.id}]", style=_ID_COLOR)

            if vertical:
                indent = " " * (2 + num_width + 1)
                text.append(f"\n{indent}", style=right_style)
                text.append(right_parts[i], style=right_style)
            else:
                right = right_parts[i].rjust(max_right)
                padding = max(max_name_width - len(name) + 2, 2)
                gap = " " * padding
                text.append(gap)
                text.append(right, style=right_style)

        self.query_one("#rl-list", Static).update(text)

    def _render_detail(self) -> None:
        if not self._resources:
            return
        idx = min(self.cursor, len(self._resources) - 1)
        resource = self._resources[idx]

        panel = self.query_one("#rl-detail-panel", Vertical)
        panel.border_title = f"Resource {idx + 1}"

        self.query_one("#rl-name", Static).update(resource.name)

        parts = [f"Preference: {resource.loading_preference.value}"]
        if resource.estimated_tokens is not None:
            parts.append(f"Tokens: ~{resource.estimated_tokens:,}")
        if resource.created_at is not None:
            parts.append(f"Created: {resource.created_at:%Y-%m-%d}")
        self.query_one("#rl-meta", Static).update("  ".join(parts))

        summary = resource.summary or "(no summary)"
        self.query_one("#rl-summary", Static).update(summary)

    # ------------------------------------------------------------------
    # Focus changes
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        if self._resources:
            self.call_after_refresh(self._render_list)

    def on_blur(self) -> None:
        if self._resources:
            self.call_after_refresh(self._render_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if self._resources and self.cursor > 0:
            self.cursor -= 1

    def action_cursor_down(self) -> None:
        if self._resources and self.cursor < len(self._resources) - 1:
            self.cursor += 1
