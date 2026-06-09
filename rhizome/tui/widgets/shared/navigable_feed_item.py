"""Navigable view bases — focus-aware ``ViewBase`` variants with a hover + focus-within border
treatment.

* ``NavigableViewBase`` — focusable ``ViewBase`` with the hover + focus-within border behaviour.
* ``NavigableFeedItemViewBase`` — additionally pins a persistent "ctrl+↑/↓" hint in the border's
  bottom-right; designed for chat-pane feed widgets that participate in
  ``ConversationAreaModel.navigate_feed()``.

Both contribute:
  * a solid dim-grey border by default, brighter on mouse hover, a gentle blue border whenever
    focus is inside the widget (self or any descendant). Both the hover and focus colors are
    driven by inline ``styles.border`` rather than the ``:hover`` / ``:focus-within`` pseudo-
    selectors — pseudo-classes invalidate descendant selectors defensively in Textual's CSS
    engine, causing every child widget's styles to be reapplied on every state change inside
    the subtree. Inline styles are node-scoped, so the cascade doesn't fire.
  * ``can_focus = True`` so the widget itself is a focus target.

The bases are *appearance + focusability only* — they don't touch the VM beyond what ``ViewBase``
already does. Whether a chat-pane feed entry participates in navigation is still governed by
``is_navigable = True`` on the VM (read by ``ConversationAreaModel.navigate_feed()``);
``NavigableFeedItemViewBase``'s job is to make the participating widget look the part.

Usage::

    class CommitProposal(NavigableFeedItemViewBase[CommitProposalModel]):
        ...

``NavigableFeedItemViewBase`` subclasses can override ``DEFAULT_NAV_HINT`` for a different hint
string, or assign ``self.border_subtitle`` themselves in ``compose`` / ``on_mount`` for fully
custom text.
"""

from __future__ import annotations

from typing import Any

from textual.events import Blur, DescendantBlur, DescendantFocus, Enter, Focus, Leave
from textual.widget import Widget

from rhizome.app.model import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


class NavigableViewBase[T: ViewModelBase](ViewBase[T]):
    """Focusable ``ViewBase`` with hover + focus-within border behaviour. See module docstring."""

    can_focus = True

    # Default border ships in CSS so the widget paints correctly before any interaction (hover /
    # focus watchers only fire on state transitions). Once interaction kicks in the inline border
    # always wins on specificity, so this rule is effectively the "untouched" fallback.
    DEFAULT_CSS = """
    NavigableViewBase {
        border: solid rgb(60, 60, 60);
    }
    """

    # ------------------------------------------------------------------
    # Border state — all driven by inline ``styles.border`` to avoid the descendant cascade
    # ------------------------------------------------------------------
    #
    # Focus events use Textual's MRO-walking ``on_<event>`` dispatch, so ``ViewBase.on_focus`` /
    # ``on_blur`` (which notify the VM) still fire automatically and we deliberately do NOT call
    # ``super()`` here (doing so double-fires).
    #
    # ``Enter`` / ``Leave`` are used (not the ``mouse_hover`` reactive) because the reactive only
    # flips ``True`` when this widget is the *topmost* under the mouse — i.e. only when the cursor
    # is in regions not occupied by a child. ``Enter`` / ``Leave`` bubble, so we receive them for
    # any descendant too, and consult ``app.mouse_over`` (the canonical top widget, updated before
    # the event is dispatched) to decide whether the mouse is anywhere in our subtree.

    def on_focus(self, event: Focus) -> None:
        self._sync_border()

    def on_blur(self, event: Blur) -> None:
        self._sync_border()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self._sync_border()

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        self._sync_border()

    def on_enter(self, event: Enter) -> None:
        self._sync_border()

    def on_leave(self, event: Leave) -> None:
        self._sync_border()

    def _sync_border(self) -> None:
        focused = self.screen.focused if self.screen else None
        focus_inside = focused is not None and (focused is self or self in focused.ancestors_with_self)
        mouse_over = self.app.mouse_over if self.screen else None
        hover_inside = mouse_over is not None and (mouse_over is self or self in mouse_over.ancestors_with_self)
        if focus_inside:
            self.styles.border = ("solid", "rgb(90, 140, 200)")
        elif hover_inside:
            self.styles.border = ("solid", "rgb(120, 120, 120)")
        else:
            self.styles.border = ("solid", "rgb(60, 60, 60)")


class NavigableFeedItemViewBase[T: ViewModelBase](NavigableViewBase[T]):
    """Adds the persistent "ctrl+↑/↓" hint to ``NavigableViewBase``'s border. See module docstring."""

    # Bottom-right border hint. Subclasses can override for a different label, or assign
    # ``self.border_subtitle`` directly after ``super().__init__`` for instance-specific text.
    DEFAULT_NAV_HINT = "ctrl+↑/↓"

    def __init__(self, vm: T, *children: Widget, **kwargs: Any) -> None:
        super().__init__(vm, *children, **kwargs)
        self.border_subtitle = self.DEFAULT_NAV_HINT
