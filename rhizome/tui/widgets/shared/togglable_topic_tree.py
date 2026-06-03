"""TogglableTopicTree — topic tree with multi-select checkboxes."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from textual.binding import Binding
from textual.message import Message
from textual.widgets._tree import TreeNode, TOGGLE_STYLE

from rhizome.db import Topic
from .topic_tree import TopicTree

_CHECKED_COLOR = Style(color="rgb(100,200,100)")
_UNCHECKED_COLOR = Style(color="rgb(80,80,80)")
_CURSOR_FOCUSED = Style(color="rgb(255,80,80)", bold=True)
_CURSOR_UNFOCUSED = Style(color="rgb(255,80,80)")


class TogglableTopicTree(TopicTree):
    """Topic tree where each node has a checkbox togglable with Space.

    Posts ``Confirmed`` with the set of selected topic IDs when the user
    presses Ctrl+Enter.
    """

    DEFAULT_CSS = """
    TogglableTopicTree {
        background: transparent;
    }
    TogglableTopicTree:focus {
        background-tint: transparent;
    }
    TogglableTopicTree > .tree--cursor {
        background: transparent;
    }
    TogglableTopicTree:focus > .tree--cursor {
        background: transparent;
    }
    TogglableTopicTree > .tree--highlight {
        background: transparent;
    }
    TogglableTopicTree > .tree--highlight-line {
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_check", show=False),
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
    ]

    class Confirmed(Message):
        """Posted when the user presses Ctrl+Enter to confirm selection."""

        def __init__(self, selected_ids: set[int]) -> None:
            super().__init__()
            self.selected_ids = set(selected_ids)

    def __init__(self, session_factory=None, **kwargs) -> None:
        super().__init__(session_factory=session_factory, **kwargs)
        self._selected_ids: set[int] = set()

    @property
    def selected_ids(self) -> set[int]:
        return set(self._selected_ids)

    def action_toggle_check(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        tid = node.data.id
        if tid in self._selected_ids:
            self._selected_ids.discard(tid)
        else:
            self._selected_ids.add(tid)
        self._invalidate_label_cache()

    def action_confirm(self) -> None:
        self.post_message(self.Confirmed(self._selected_ids))

    def render_label(
        self, node: TreeNode[Topic], base_style: Style, style: Style,
    ) -> Text:
        # Build the expand/collapse icon prefix.
        if node._allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_STYLE
        else:
            icon = ""
            icon_style = base_style

        # Checkbox
        if node.data is not None:
            checked = node.data.id in self._selected_ids
            checkbox = "[x] " if checked else "[ ] "
            checkbox_style = _CHECKED_COLOR if checked else _UNCHECKED_COLOR
        else:
            checkbox = ""
            checkbox_style = style

        # Cursor styling: bold red when focused, red when blurred.
        is_cursor = node is self.cursor_node
        if is_cursor:
            label_style = _CURSOR_FOCUSED if self.has_focus else _CURSOR_UNFOCUSED
        else:
            label_style = style

        node_label = node._label.copy()
        node_label.stylize(label_style)

        text = Text.assemble(
            (icon, icon_style),
            (checkbox, base_style + checkbox_style),
            node_label,
        )

        if self.show_ids and node.data is not None:
            text.append(f"  [{node.data.id}]", style=Style(color="rgb(80,80,80)"))

        return text
