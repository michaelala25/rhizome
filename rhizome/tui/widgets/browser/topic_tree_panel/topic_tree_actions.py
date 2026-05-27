"""TopicTreeActionsViewModel + TopicTreeActionsView — vertical action menu for the topic tree.

Sits to the left of the tree inside the panel body. Collapsed by default (single-letter shorthand,
no cursor marker, narrow rail); on focus, the widget renders full ``► label`` rows and toggles
``-actions-expanded`` on the surrounding ``TopicTreePanelView`` so the panel CSS widens the rail.

VM action methods are stubs at this stage — they log the cursor / selection state. Concrete
dialogs and DB calls land in follow-up passes; the bones here are layout, focus routing, and the
``ChoiceList`` dispatch protocol.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from rhizome.logs import get_logger

from ...view_model_base import ViewModelBase
from ..choices import ChoiceList
from ..topic_tree import BrowserTopicTreeViewModel

_logger = get_logger("browser.topic_tree_actions")


class TopicTreeActionsViewModel(ViewModelBase):
    """Holds a reference to the tree VM so action stubs can read cursor / selection state directly
    instead of routing through the orchestrator."""

    def __init__(self, session_factory: Any, tree_vm: BrowserTopicTreeViewModel) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = tree_vm

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    async def rename_topic(self) -> None:
        _logger.info("rename_topic stub — cursor=%s", self._tree.cursor_topic_id)

    async def create_topic(self) -> None:
        _logger.info("create_topic stub — cursor parent=%s", self._tree.cursor_topic_id)

    async def delete_topic(self) -> None:
        _logger.info(
            "delete_topic (subtree) stub — cursor=%s, selection=%d",
            self._tree.cursor_topic_id,
            len(self._tree.selected_ids),
        )


class TopicTreeActionsView(ChoiceList[TopicTreeActionsViewModel]):
    """Vertical ``ChoiceList`` rendered to the left of the tree. Overrides ``_render_choice`` to
    show a single-letter shorthand when blurred and the full ``► label`` when focused, and toggles
    the panel's ``-actions-expanded`` class on focus/blur so the rail width follows."""

    ORIENTATION = "vertical"
    CHOICES = {
        "rename": "do_rename",
        "create": "do_create",
        "delete": "do_delete",
    }

    DEFAULT_CSS = """
    /* Collapsed default: zero horizontal padding so the shorthand letter sits flush against both
       sides — the visual breathing room around the rule comes from the *tree*'s left-padding. */
    TopicTreeActionsView {
        width: auto;
        height: 1fr;
        padding: 1 0 0 0;
    }
    /* Expanded: add horizontal padding so the labels don't crowd the rule. Driven by the same
       ``-actions-expanded`` class the rail width is keyed off, toggled on the panel view. */
    TopicTreePanelView.-actions-expanded TopicTreeActionsView {
        padding: 1 2 0 1;
    }
    """

    _COLLAPSED_SHORTHAND = {
        "rename": "R",
        "create": "C",
        "delete": "D",
    }

    def _render_choice(self, label: str, selected: bool) -> Text:
        text = Text()
        if self.has_focus:
            cursor_color = "bold #ffd700"
            if selected:
                text.append("► ", style=cursor_color)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
        else:
            # Blurred: shorthand only, no cursor marker (cursor reappears at its retained index on
            # next focus — standard restore-on-focus behaviour).
            display = self._COLLAPSED_SHORTHAND.get(label, label[:1].upper())
            text.append(display, style="dim")
        return text

    def on_focus(self) -> None:
        super().on_focus()
        self._set_pane_expanded(True)

    def on_blur(self) -> None:
        super().on_blur()
        self._set_pane_expanded(False)

    def _set_pane_expanded(self, expanded: bool) -> None:
        # Type-name string query (not a class import) to avoid the circular import the panel view
        # induces by importing this widget. Best-effort: if the ancestor isn't mounted yet during
        # compose-time focus, silently skip — Textual will fire focus again post-mount.
        try:
            pane = self.screen.query_one("TopicTreePanelView")
        except Exception:
            return
        pane.set_class(expanded, "-actions-expanded")

    # ChoiceList.action_confirm resolves these by name via getattr.
    async def do_rename(self) -> None:
        await self._vm.rename_topic()

    async def do_create(self) -> None:
        await self._vm.create_topic()

    async def do_delete(self) -> None:
        await self._vm.delete_topic()
