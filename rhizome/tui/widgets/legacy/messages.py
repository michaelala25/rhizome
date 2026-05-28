"""Shared widget messages."""

from __future__ import annotations

from textual.message import Message

from rhizome.db import Topic


class ActiveTopicChanged(Message):
    """Posted when the user selects or clears the active topic.

    Used by both ExplorerViewer and ResourceViewer so ChatPane can
    handle topic changes with a single handler.
    """

    def __init__(self, topic: Topic | None, path: list[str]) -> None:
        super().__init__()
        self.topic = topic
        self.path = path
