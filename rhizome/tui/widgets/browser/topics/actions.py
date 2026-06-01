"""ActionMenu — vertical action menu for the topic tree.

Sits to the left of the tree inside the panel body and always renders in expanded form (``Actions``
header + full ``► label`` rows). The ``-actions-expanded`` class on the surrounding
``TopicTreePanel`` is set at panel construction time; the auto-toggle on focus / blur that used to
narrow the rail when the menu lost focus is retired. The class and ``_set_pane_expanded`` method
remain as a skeleton so a manual collapse path can be re-introduced later — re-enabling the
rendering switch is the missing piece on top of what's here today.

This view is intentionally **VM-less** — the menu has no data-model state of its own and exists
solely to announce action requests upward. Each entry in ``CHOICES`` resolves to an action method
that posts one of the nested ``Requested`` messages; the surrounding ``TopicTreePanel`` catches
them through Textual's message pump (``on_action_menu_view_<name>_requested``).
"""

from __future__ import annotations

from rich.text import Text

from textual.message import Message

from rhizome.tui.widgets.shared.choices_list import ChoiceList


class ActionMenu(ChoiceList[None]):
    """Vertical ``ChoiceList`` rendered to the left of the tree. Always renders in expanded form
    (``Actions`` header + full ``► label`` rows); see module docstring for the retired collapse
    behaviour."""

    ORIENTATION = "vertical"
    CHOICES = {
        "rename": "_rename",
        "create": "_create",
        "delete": "_delete",
    }

    DEFAULT_CSS = """
    /* Permanent expanded padding — horizontal room for the labels and the cursor marker. The
       panel's ``-actions-expanded`` class is set at construction and isn't auto-toggled, so the
       class-keyed override that used to drive this padding is gone. */
    ActionMenu {
        width: auto;
        height: 1fr;
        padding: 1 2 0 1;
    }
    """

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

    def _render_header(self) -> Text | None:
        # Plain (non-bold) text in the default foreground — pairs visually with the panel-level
        # bold ``Topics`` title across the body.
        return Text("Actions")

    def _set_pane_expanded(self, expanded: bool) -> None:
        # Skeleton — no longer called from focus/blur. Retained as the manual hook for re-enabling
        # collapse later; bringing the rail back also requires restoring the renderer's
        # expanded-vs-collapsed gating, since ``_render_choice`` no longer responds to it.
        try:
            pane = self.screen.query_one("TopicTreePanel")
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
