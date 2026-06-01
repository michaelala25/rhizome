"""NavigableFeedItemViewBase — ``ViewBase`` variant for chat-pane feed widgets that participate in
ctrl+up/down navigation.

Drop-in replacement for ``ViewBase[T]``: inherits its dirty/focus subscription wiring and
``on_focus``/``on_blur`` forwarding, and additionally contributes:
  * a solid dim-grey border by default, brighter on hover; a gentle blue border whenever focus is
    inside the widget (self or any descendant). The focus color is driven by inline
    ``styles.border`` rather than the ``:focus-within`` pseudo-selector — pseudo-classes invalidate
    descendant selectors defensively in Textual's CSS engine, causing every child widget's styles
    to be reapplied on every focus shift inside the subtree. Inline styles are node-scoped, so
    the cascade doesn't fire.
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

from textual.events import Blur, DescendantBlur, DescendantFocus, Focus
from textual.widget import Widget

from rhizome.app.vm import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


class NavigableFeedItemViewBase[T: ViewModelBase](ViewBase[T]):
    """Navigable-feed-item flavoured ``ViewBase``. See module docstring."""

    can_focus = True

    # Bottom-right border hint. Subclasses can override for a different label, or assign
    # ``self.border_subtitle`` directly after ``super().__init__`` for instance-specific text.
    DEFAULT_NAV_HINT = "ctrl+↑/↓"

    # Focus border color is set inline in ``_sync_focus_border`` rather than via a CSS rule, so the
    # default + hover styles stay in CSS while the focus state bypasses the descendant cascade.
    DEFAULT_CSS = """
    NavigableFeedItemViewBase {
        border: solid rgb(60, 60, 60);
    }
    NavigableFeedItemViewBase:hover {
        border: solid rgb(120, 120, 120);
    }
    """

    def __init__(self, vm: T, *children: Widget, **kwargs: Any) -> None:
        super().__init__(vm, *children, **kwargs)
        self.border_subtitle = self.DEFAULT_NAV_HINT

    # ------------------------------------------------------------------
    # Focus-within border — inline ``styles.border`` to avoid the descendant cascade
    # ------------------------------------------------------------------
    #
    # All four focus events funnel into ``_sync_focus_border``: the two self events (on_focus /
    # on_blur) cover the case where this widget is the focus target, the two descendant events
    # cover focus shifts within the subtree. Textual auto-dispatches named ``on_<event>`` handlers
    # at every MRO level — so ``ViewBase.on_focus`` / ``on_blur`` (which notify the VM) still fire
    # automatically, and we deliberately do NOT call ``super()`` here (doing so double-fires).

    def on_focus(self, event: Focus) -> None:
        self._sync_focus_border()

    def on_blur(self, event: Blur) -> None:
        self._sync_focus_border()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self._sync_focus_border()

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        self._sync_focus_border()

    def _sync_focus_border(self) -> None:
        focused = self.screen.focused if self.screen else None
        inside = focused is not None and (focused is self or self in focused.ancestors_with_self)
        # ``None`` clears the inline border so the CSS default / ``:hover`` rules take over.
        self.styles.border = ("solid", "rgb(90, 140, 200)") if inside else None
