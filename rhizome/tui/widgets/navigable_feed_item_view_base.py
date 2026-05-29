"""NavigableFeedItemViewBase — ``ViewBase`` variant for chat-pane feed widgets that participate in
ctrl+up/down navigation.

Drop-in replacement for ``ViewBase[T]``: inherits its dirty/focus subscription wiring and
``on_focus``/``on_blur`` forwarding, and additionally contributes:
  * a solid dim-grey border by default, brighter on hover, gentle blue on ``:focus`` /
    ``:focus-within`` (the latter so composite widgets light up when an inner child gets focus)
  * ``can_focus = True`` so the widget itself is a focus target for ``ChatPaneVM.navigate_feed()``
  * a persistent "ctrl+↑/↓" nav hint in the border's bottom-right via Textual's ``border_subtitle``

The base is *appearance + focusability only* — it doesn't touch the VM beyond what ``ViewBase``
already does. Whether a feed entry participates in navigation is still governed by
``is_navigable = True`` on the VM (read by ``ChatPaneVM.navigate_feed()``); this base's job is to
make the participating widget look the part.

Usage::

    class CommitProposal(NavigableFeedItemViewBase[CommitProposalVM]):
        ...

Subclasses can override ``DEFAULT_NAV_HINT`` for a different hint string, or assign
``self.border_subtitle`` themselves in ``compose`` / ``on_mount`` for fully custom text.
"""

from __future__ import annotations

from typing import Any

from textual.widget import Widget

from rhizome.app.vm import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


class NavigableFeedItemViewBase[T: ViewModelBase](ViewBase[T]):
    """Navigable-feed-item flavoured ``ViewBase``. See module docstring."""

    can_focus = True

    # Bottom-right border hint. Subclasses can override for a different label, or assign
    # ``self.border_subtitle`` directly after ``super().__init__`` for instance-specific text.
    DEFAULT_NAV_HINT = "ctrl+↑/↓"

    DEFAULT_CSS = """
    NavigableFeedItemViewBase {
        border: solid rgb(60, 60, 60);
    }
    NavigableFeedItemViewBase:hover {
        border: solid rgb(120, 120, 120);
    }
    NavigableFeedItemViewBase:focus, NavigableFeedItemViewBase:focus-within {
        border: solid rgb(90, 140, 200);
    }
    """

    def __init__(self, vm: T, *children: Widget, **kwargs: Any) -> None:
        super().__init__(vm, *children, **kwargs)
        self.border_subtitle = self.DEFAULT_NAV_HINT
