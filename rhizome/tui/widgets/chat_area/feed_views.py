"""Registration manifest for chat_area feed views.

Importing this populates the (shared) feed-view registry for chat_area's feed. chat_area reuses every
chat_pane feed widget wholesale — the feed VMs are bridge-imported — so this just pulls in the chat_pane
manifest for that side effect and adds the one chat_area-specific override: the ``BranchPoint`` bound to
chat_area's ``BranchPointModel``. Import before calling ``view_for``.
"""

from __future__ import annotations

# Shared feed widgets (chat message, agent message, tool list, thinking indicator, interrupts, ...).
from rhizome.tui.widgets.chat_pane import feed_views  # noqa: F401
# chat_area's own BranchPoint — registered for chat_area's BranchPointModel by importing it.
from rhizome.tui.widgets.chat_area import branch  # noqa: F401
