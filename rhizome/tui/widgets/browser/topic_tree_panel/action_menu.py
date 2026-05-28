"""ActionMenuView — vertical action menu for the topic tree.

Sits to the left of the tree inside the panel body. Collapsed by default (single-letter shorthand,
no cursor marker, narrow rail); on focus, the widget renders full ``► label`` rows and toggles
``-actions-expanded`` on the surrounding ``TopicTreePanelView`` so the panel CSS widens the rail.

This view is intentionally **VM-less** — the menu has no data-model state of its own and exists
solely to announce action requests upward. Each entry in ``CHOICES`` resolves to an action method
that posts one of the nested ``Requested`` messages; the surrounding ``TopicTreePanelView`` catches
them through Textual's message pump (``on_action_menu_view_<name>_requested``).
"""

from __future__ import annotations

from rich.text import Text

from textual.message import Message

from ..choices import ChoiceList


class ActionMenuView(ChoiceList[None]):
    """Vertical ``ChoiceList`` rendered to the left of the tree. Overrides ``_render_choice`` to
    show a single-letter shorthand when blurred and the full ``► label`` when focused, and toggles
    the panel's ``-actions-expanded`` class on focus/blur so the rail width follows."""

    ORIENTATION = "vertical"
    CHOICES = {
        "rename": "_rename",
        "create": "_create",
        "delete": "_delete",
    }

    DEFAULT_CSS = """
    /* Collapsed default: zero horizontal padding so the shorthand letter sits flush against both
       sides — the visual breathing room around the rule comes from the *tree*'s left-padding. */
    ActionMenuView {
        width: auto;
        height: 1fr;
        padding: 1 0 0 0;
    }
    /* Expanded: add horizontal padding so the labels don't crowd the rule. Driven by the same
       ``-actions-expanded`` class the rail width is keyed off, toggled on the panel view. */
    TopicTreePanelView.-actions-expanded ActionMenuView {
        padding: 1 2 0 1;
    }
    """

    _COLLAPSED_SHORTHAND = {
        "rename": "r",
        "create": "c",
        "delete": "d",
    }

    # ------------------------------------------------------------------
    # Messages — one per action; the panel handles them via on_<snake>.
    # ------------------------------------------------------------------

    class RenameRequested(Message):
        """User picked ``rename`` from the action menu."""

    class CreateRequested(Message):
        """User picked ``create`` from the action menu."""

    class DeleteRequested(Message):
        """User picked ``delete`` from the action menu."""

    def __init__(self, **kwargs) -> None:
        super().__init__(view_model=None, **kwargs)

    def _render_choice(self, label: str, selected: bool) -> Text:
        # When focused the base's ``► bold`` / ``  dim`` rendering is exactly what we want; only the
        # blurred state diverges (single-letter shorthand, no cursor marker — cursor reappears at
        # its retained index on next focus).
        if self.has_focus:
            return super()._render_choice(label, selected)
        display = self._COLLAPSED_SHORTHAND.get(label, label[:1].upper())
        return Text(display, style="dim")

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
    def _rename(self) -> None:
        self.post_message(self.RenameRequested())

    def _create(self) -> None:
        self.post_message(self.CreateRequested())

    def _delete(self) -> None:
        self.post_message(self.DeleteRequested())
