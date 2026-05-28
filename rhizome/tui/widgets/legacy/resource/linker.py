"""ResourceLinker — checkbox list for linking/unlinking resources to a topic."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rhizome.db import Resource
from rhizome.tui.dock import DockableWidgetMixin
from rhizome.tui.types import Arrangement
from rhizome.tui.widgets.legacy.resource.view_model import ResourceLinkerViewModel

_DIM = "rgb(100,100,100)"
_ALT_GREY = "rgb(180,180,180)"
_FOCUS_GREEN = "rgb(100,200,100)"
_ALT_BG_1 = "rgb(25,25,25)"
_ALT_BG_2 = "rgb(35,35,35)"
_CHECKED_COLOR = "rgb(100,200,100)"
_UNCHECKED_COLOR = "rgb(80,80,80)"
_ID_COLOR = "rgb(80,80,80)"


class ResourceLinker(Widget, DockableWidgetMixin, can_focus=True):
    """Checkbox list of all resources for linking/unlinking to a topic."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("space", "toggle_link", show=False),
        Binding("enter", "toggle_link", show=False),
    ]

    DEFAULT_CSS = """
    ResourceLinker {
        height: auto;
        layout: vertical;
        padding: 0 1;
    }
    ResourceLinker #rlk-list-scroll {
        height: auto;
        max-height: 20;
        margin: 1 0 1 0;
    }
    ResourceLinker #rlk-list {
        height: auto;
    }
    ResourceLinker #rlk-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 1;
    }
    ResourceLinker #rlk-hint {
        color: rgb(80,80,80);
        margin: 0 0 0 1;
    }
    """

    class LinkToggled(Message):
        """Posted when a resource link is toggled."""

        def __init__(self, resource: Resource, linked: bool) -> None:
            super().__init__()
            self.resource = resource
            self.linked = linked

    cursor: reactive[int] = reactive(0)
    show_ids: reactive[bool] = reactive(False)

    def __init__(self, view_model: ResourceLinkerViewModel | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vm = view_model or ResourceLinkerViewModel()

    # -- Properties that read/write through to the view model -------------

    @property
    def _resources(self) -> list[Resource]:
        return self._vm.resources

    @_resources.setter
    def _resources(self, value: list[Resource]) -> None:
        self._vm.resources = value

    @property
    def _linked_ids(self) -> set[int]:
        return self._vm.linked_ids

    @_linked_ids.setter
    def _linked_ids(self, value: set[int]) -> None:
        self._vm.linked_ids = value

    def compose(self) -> ComposeResult:
        yield Static("", id="rlk-empty")
        with VerticalScroll(id="rlk-list-scroll"):
            yield Static(id="rlk-list")
        yield Static("", id="rlk-hint")

    def on_mount(self) -> None:
        self.show_ids = self._vm.show_ids
        self.cursor = self._vm.cursor
        self._apply_empty_state()
        if self._resources:
            self._render_list()
            self._update_hint()
            self._scroll_cursor_visible()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_resources(self, resources: list[Resource], linked_ids: set[int]) -> None:
        """Replace the displayed resources with link state."""
        self._resources = list(resources)
        self._linked_ids = set(linked_ids)
        self.cursor = 0
        self._apply_empty_state()
        if self._resources:
            self._render_list()
            self._update_hint()
            self._scroll_cursor_visible()

    def update_linked_ids(self, linked_ids: set[int]) -> None:
        """Update which resources are linked without replacing the list."""
        self._linked_ids = set(linked_ids)
        if self._resources:
            self._render_list()
            self._update_hint()

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_cursor(self) -> None:
        self._vm.cursor = self.cursor
        if self._resources:
            self._render_list()
            self._update_hint()
            self._scroll_cursor_visible()

    def watch_show_ids(self) -> None:
        self._vm.show_ids = self.show_ids
        if self._resources:
            self._render_list()

    def _scroll_cursor_visible(self) -> None:
        self.call_after_refresh(self._do_scroll_cursor_visible)

    def _do_scroll_cursor_visible(self) -> None:
        scroll = self.query_one("#rlk-list-scroll", VerticalScroll)
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
        self.query_one("#rlk-empty", Static).display = empty
        self.query_one("#rlk-list-scroll", VerticalScroll).display = not empty
        self.query_one("#rlk-hint", Static).display = not empty
        if empty:
            self.query_one("#rlk-empty", Static).update("(No resources in database)")

    def _render_list(self) -> None:
        # Compute max name width from available space.
        # Layout: marker(2) + checkbox(4) + name + id?
        total_width = self.size.width - 2  # padding
        id_overhead = max((len(str(r.id)) + 4 for r in self._resources), default=0) if self.show_ids else 0
        max_name_width = total_width - 2 - 4 - id_overhead
        max_name_width = max(max_name_width, 10)  # floor

        text = Text()
        for i, resource in enumerate(self._resources):
            if i > 0:
                text.append("\n")

            is_selected = self.cursor == i
            is_linked = resource.id in self._linked_ids

            checkbox = "[x] " if is_linked else "[ ] "
            checkbox_color = _CHECKED_COLOR if is_linked else _UNCHECKED_COLOR

            if is_selected and self.has_focus:
                name_style = f"bold {_FOCUS_GREEN}"
                marker = "► "
            elif is_selected:
                name_style = "bold"
                marker = "► "
            else:
                name_style = "" if i % 2 == 0 else _ALT_GREY
                marker = "  "

            name = resource.name
            vertical = self.dock_arrangement == Arrangement.VERTICAL
            if not vertical and len(name) > max_name_width:
                name = name[: max_name_width - 1] + "…"

            text.append(marker, style=name_style)
            text.append(checkbox, style=checkbox_color)
            text.append(name, style=name_style)
            if self.show_ids:
                text.append(f"  [{resource.id}]", style=_ID_COLOR)

        self.query_one("#rlk-list", Static).update(text)

    def _update_hint(self) -> None:
        linked_count = sum(1 for r in self._resources if r.id in self._linked_ids)
        total = len(self._resources)
        self.query_one("#rlk-hint", Static).update(
            f"{linked_count}/{total} linked  |  space/enter: toggle"
        )

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

    def action_toggle_link(self) -> None:
        if not self._resources:
            return
        idx = min(self.cursor, len(self._resources) - 1)
        resource = self._resources[idx]
        if resource.id in self._linked_ids:
            self._linked_ids.discard(resource.id)
            linked = False
        else:
            self._linked_ids.add(resource.id)
            linked = True
        self._render_list()
        self._update_hint()
        self.post_message(self.LinkToggled(resource, linked))
