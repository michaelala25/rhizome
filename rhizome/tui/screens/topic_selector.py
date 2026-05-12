"""TopicSelectorScreen — lightweight modal for selecting a topic."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, Tree

from rhizome.tui.widgets import TopicTree


class TopicSelectorScreen(ModalScreen[tuple[int, str] | None]):
    """Modal screen for picking a topic from the tree.

    Dismisses with ``(topic_id, topic_name)`` on selection, or ``None`` on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("enter,ctrl+j", "select", show=False),
    ]

    def __init__(self, *, session_factory=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session_factory = session_factory

    DEFAULT_CSS = """
    TopicSelectorScreen {
        align: center middle;
    }
    TopicSelectorScreen > Vertical {
        width: 60;
        height: auto;
        max-height: 80%;
        border: solid $surface-lighten-2;
        padding: 1 2;
        background: $surface;
    }
    TopicSelectorScreen Static {
        color: rgb(100,100,100);
        margin-bottom: 1;
    }
    """

    def compose(self):
        with Vertical():
            yield Static("Select a topic  (arrows navigate, ctrl+enter select, esc cancel)")
            yield TopicTree(session_factory=self._session_factory)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is not None:
            self.dismiss((event.node.data.id, event.node.data.name))

    def action_select(self) -> None:
        tree = self.query_one(TopicTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            self.dismiss((node.data.id, node.data.name))

    def action_cancel(self) -> None:
        self.dismiss(None)
