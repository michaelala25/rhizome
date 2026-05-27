"""TopicTreeActionsViewModel + TopicTreeActionsView — action menu for the topic tree.

Sits as a sibling of the topic tree inside ``#browser-tree-tab``: a narrow column of vertically-stacked
choices (rename / create / delete, more to follow) that the user navigates to via ``alt+left`` from the
tree and back to the tree via ``alt+right``.

Collapsed by default; expands to show full labels when focused. Expansion is two-stage:

  * the menu widget itself swaps its render between "marker only" (collapsed) and "marker + label"
    (expanded), and
  * the parent ``#browser-tree-tab`` gains a ``-actions-expanded`` class on focus, which CSS uses to
    roughly double the pane width so the labels have room to breathe.

The VM is a thin shell — each action method is a stub that logs and returns. Concrete handlers (open
a rename dialog, run ``delete_topic`` against the cascade, etc.) land in follow-up passes; the bones
here are about layout, focus routing, and the dispatch protocol, not the actual behaviour.
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
    """VM for the topic-tree action menu.

    Holds a reference to the topic tree VM so action methods can read the cursor / selection without
    routing every call through the orchestrator. Methods are deliberately stubs at this stage — they
    log enough context (cursor topic id, selection size) to make the wiring traceable from logs while
    the concrete dialogs and DB calls are still being designed.
    """

    def __init__(self, session_factory: Any, tree_vm: BrowserTopicTreeViewModel) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tree = tree_vm

    @property
    def tree(self) -> BrowserTopicTreeViewModel:
        return self._tree

    # ------------------------------------------------------------------
    # Action stubs
    # ------------------------------------------------------------------

    async def rename_topic(self) -> None:
        _logger.info(
            "rename_topic stub — cursor=%s", self._tree.cursor_topic_id
        )

    async def create_topic(self) -> None:
        _logger.info(
            "create_topic stub — cursor parent=%s", self._tree.cursor_topic_id
        )

    async def delete_topic(self) -> None:
        _logger.info(
            "delete_topic (subtree) stub — cursor=%s, selection=%d",
            self._tree.cursor_topic_id,
            len(self._tree.selected_ids),
        )


class TopicTreeActionsView(ChoiceList[TopicTreeActionsViewModel]):
    """Vertical action menu rendered to the left of the topic tree.

    Subclasses ``ChoiceList`` for the cursor + dispatch + focus-brightness kernel. Customisations:

      * ``ORIENTATION = "vertical"`` — stack the options.
      * Overrides ``_render_choice`` so the collapsed (unfocused) state shows just the cursor marker
        and a single-letter shorthand, while the focused state shows the full label.
      * ``on_focus`` / ``on_blur`` toggle a ``-actions-expanded`` class on ``#browser-tree-tab`` so the
        pane CSS can widen the whole rail while the menu is in use.

    The action method names match ``CHOICES`` values — ``ChoiceList.action_confirm`` resolves them via
    ``getattr(self, action_name)`` on Enter. Each delegates to a VM stub for now.
    """

    ORIENTATION = "vertical"
    CHOICES = {
        "rename": "do_rename",
        "create": "do_create",
        "delete": "do_delete",
    }

    DEFAULT_CSS = """
    /* Collapsed default: zero horizontal padding so the shorthand letter sits flush against both
       sides — the visual breathing room comes from the *tree*'s left-padding (which pushes tree
       content away from the rule), not from the menu. */
    TopicTreeActionsView {
        width: auto;
        height: 1fr;
        padding: 1 0 0 0;
    }
    /* Expanded: add horizontal padding so the labels breathe and don't crowd the rule. Driven by
       the same ``-actions-expanded`` class the rail width is keyed off, toggled on the surrounding
       ``TopicTreePanelView``. */
    TopicTreePanelView.-actions-expanded TopicTreeActionsView {
        padding: 1 2 0 1;
    }
    """

    # Single-letter shorthands shown in the collapsed (unfocused) state. Keyed by the same label used
    # in CHOICES so a rename of either side is a one-line edit.
    _COLLAPSED_SHORTHAND = {
        "rename": "R",
        "create": "C",
        "delete": "D",
    }

    def _render_choice(self, label: str, selected: bool) -> Text:
        text = Text()
        if self.has_focus:
            # Expanded: standard ``► label`` (cursor) / ``  label`` (other).
            cursor_color = "bold #ffd700"
            if selected:
                text.append("► ", style=cursor_color)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
        else:
            # Collapsed: just the single-letter shorthand, no cursor marker. The cursor is dormant
            # while the widget is blurred (focusing it later re-renders with the cursor visible at
            # whatever index it was left on, which is the conventional restore-on-focus behaviour).
            display = self._COLLAPSED_SHORTHAND.get(label, label[:1].upper())
            text.append(display, style="dim")
        return text

    # ------------------------------------------------------------------
    # Focus → pane expansion
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        super().on_focus()
        self._set_pane_expanded(True)

    def on_blur(self) -> None:
        super().on_blur()
        self._set_pane_expanded(False)

    def _set_pane_expanded(self, expanded: bool) -> None:
        """Toggle ``-actions-expanded`` on the surrounding ``TopicTreePanelView`` so the panel CSS
        can widen the rail while the menu is in use. The query uses the type-name string rather
        than importing the class to avoid a circular import (the panel view imports this widget).
        Best-effort: if the ancestor isn't mounted yet (focus during compose), we silently skip —
        Textual will fire focus again post-mount."""
        try:
            pane = self.screen.query_one("TopicTreePanelView")
        except Exception:
            return
        pane.set_class(expanded, "-actions-expanded")

    # ------------------------------------------------------------------
    # Action handlers — invoked by ChoiceList.action_confirm via getattr
    # ------------------------------------------------------------------

    async def do_rename(self) -> None:
        await self._vm.rename_topic()

    async def do_create(self) -> None:
        await self._vm.create_topic()

    async def do_delete(self) -> None:
        await self._vm.delete_topic()
