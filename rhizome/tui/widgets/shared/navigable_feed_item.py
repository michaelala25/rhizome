"""Navigable view bases — focus-aware ``ViewBase`` variants with a focus-within border treatment.

* ``NavigableViewBase`` — focusable ``ViewBase`` with the focus-within border behaviour.
* ``NavigableFeedItemViewBase`` — additionally pins a persistent "ctrl+↑/↓" hint in the border's
  bottom-right; designed for chat-pane feed widgets that participate in
  ``ChatPaneVM.navigate_feed()``.

Both contribute:
  * a solid dim-grey border by default, brighter on hover; a gentle blue border whenever focus is
    inside the widget (self or any descendant). The focus color is driven by inline
    ``styles.border`` rather than the ``:focus-within`` pseudo-selector — pseudo-classes invalidate
    descendant selectors defensively in Textual's CSS engine, causing every child widget's styles
    to be reapplied on every focus shift inside the subtree. Inline styles are node-scoped, so
    the cascade doesn't fire.
  * ``can_focus = True`` so the widget itself is a focus target.

The bases are *appearance + focusability only* — they don't touch the VM beyond what ``ViewBase``
already does. Whether a chat-pane feed entry participates in navigation is still governed by
``is_navigable = True`` on the VM (read by ``ChatPaneVM.navigate_feed()``);
``NavigableFeedItemViewBase``'s job is to make the participating widget look the part.

Usage::

    class CommitProposal(NavigableFeedItemViewBase[CommitProposalVM]):
        ...

``NavigableFeedItemViewBase`` subclasses can override ``DEFAULT_NAV_HINT`` for a different hint
string, or assign ``self.border_subtitle`` themselves in ``compose`` / ``on_mount`` for fully
custom text.
"""

from __future__ import annotations

from typing import Any

from textual.events import Blur, DescendantBlur, DescendantFocus, Focus
from textual.widget import Widget

from rhizome.app.vm import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


class NavigableViewBase[T: ViewModelBase](ViewBase[T]):
    """Focusable ``ViewBase`` with the focus-within border treatment. See module docstring."""

    can_focus = True

    # Focus border color is set inline in ``_sync_focus_border`` rather than via a CSS rule, so the
    # default + hover styles stay in CSS while the focus state bypasses the descendant cascade.
    DEFAULT_CSS = """
    NavigableViewBase {
        border: solid rgb(60, 60, 60);
    }
    NavigableViewBase:hover {
        border: solid rgb(120, 120, 120);
    }
    """

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


class NavigableFeedItemViewBase[T: ViewModelBase](NavigableViewBase[T]):
    """Adds the persistent "ctrl+↑/↓" hint to ``NavigableViewBase``'s border. See module docstring."""

    # Bottom-right border hint. Subclasses can override for a different label, or assign
    # ``self.border_subtitle`` directly after ``super().__init__`` for instance-specific text.
    DEFAULT_NAV_HINT = "ctrl+↑/↓"

    def __init__(self, vm: T, *children: Widget, **kwargs: Any) -> None:
        super().__init__(vm, *children, **kwargs)
        self.border_subtitle = self.DEFAULT_NAV_HINT
