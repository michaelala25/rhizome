"""NavigableWidgetMixin — shared focus/border/hint behavior for active-stack widgets."""

from __future__ import annotations

from typing import Any

from textual.message import Message
from textual.widget import Widget


class WidgetDeactivated(Message):
    """Posted by interactive widgets when they are no longer accepting input.

    ChatPane listens for this to remove the widget from the navigable
    active-widget stack.
    """

    def __init__(self, sender: Any) -> None:
        super().__init__()
        self.sender_widget = sender


_NAV_HINT = "ctrl+\u2191/\u2193 to navigate"


class NavigableWidgetMixin(Widget):
    """Mixin providing focus-border, navigation hint, and deactivation lifecycle.

    Border styles for the ``.navigable`` CSS class are defined in the App-level
    CSS (``RhizomeApp.CSS``) so they apply globally regardless of widget type.

    Widgets using this mixin should:

    1. Call ``setup_navigation()`` in ``on_mount`` (or inherit from a base
       that does so).
    2. Call ``deactivate_navigation()`` when they are no longer interactable.
    3. Override ``is_navigable()`` to gate subtitle restoration on blur
       (e.g. return ``False`` once a future has resolved).
    4. Call ``super().on_focus()`` / ``super().on_blur()`` if overriding
       those handlers.
    """

    def is_navigable(self) -> bool:
        """Return ``True`` while the widget is still accepting input."""
        return True

    def setup_navigation(self) -> None:
        """Initialize the navigable border and hint.  Call from ``on_mount``."""
        self.add_class("navigable")
        self.border_subtitle = _NAV_HINT

    DISABLE_CHILDREN_ON_DEACTIVATE: bool = True
    """When ``True`` (the default), ``deactivate_navigation()`` sets
    ``can_focus = False`` on every descendant widget.  Subclasses that need
    certain children to remain focusable after deactivation (e.g. a collapse
    toggle) can set this to ``False`` and manage descendants themselves."""

    def deactivate_navigation(self) -> None:
        """Clear the navigation hint and notify ChatPane."""
        self.border_subtitle = None
        self.remove_class("navigable")
        self.add_class("deactivated")
        self.can_focus = False
        if self.DISABLE_CHILDREN_ON_DEACTIVATE:
            for child in self.query("*"):
                child.can_focus = False
        self.post_message(WidgetDeactivated(self))

    # ------------------------------------------------------------------
    # Focus / blur handlers — manage the subtitle hint
    # ------------------------------------------------------------------

    def on_focus(self) -> None:
        if self.is_navigable():
            self.border_subtitle = None

    def on_blur(self) -> None:
        if self.is_navigable():
            self.border_subtitle = _NAV_HINT

    def on_descendant_focus(self, event) -> None:
        if self.is_navigable():
            self.border_subtitle = None

    def on_descendant_blur(self, event) -> None:
        if self.is_navigable():
            self.border_subtitle = _NAV_HINT
