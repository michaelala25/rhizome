"""Compatibility re-export — ``TopicTree`` lives in ``rhizome.tui.widgets.shared.topic_tree``.

Legacy modules still import it from this path; this shim keeps them resolving until the
legacy package is retired.
"""

from rhizome.tui.widgets.shared.topic_tree import TopicTree

__all__ = ["TopicTree"]
