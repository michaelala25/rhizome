"""chat_area BranchPoint — the branch-indicator widget for the rewritten conversation feed.

chat_area's ``BranchPointModel`` exposes the same surface the chat_pane indicator drives (``children`` /
``selected_child`` / ``request_descend|ascend|sibling|rename``), so the widget is reused wholesale; only
the feed-registry binding differs — this view is registered for chat_area's VM type. Folds into one class
when the old pane retires.
"""

from __future__ import annotations

from rhizome.app.chat_area.branch import BranchPointModel
from rhizome.tui.widgets.chat_pane.branch import BranchPoint as _ChatPaneBranchPoint
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view


@register_feed_view(BranchPointModel)
class BranchPoint(_ChatPaneBranchPoint):
    """chat_area branch indicator — behaviour inherited from the chat_pane widget, bound to chat_area's
    ``BranchPointModel`` via the registry decorator above (see module docstring)."""
