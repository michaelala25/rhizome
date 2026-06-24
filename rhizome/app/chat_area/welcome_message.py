"""WelcomeMessageModel — the banner shown at the top of a fresh chat feed.

A static feed entry: it holds the resolved user name and derives the greeting line from it. No mutable
state, so it never emits ``dirty`` — its view is a dumb mirror (cf. ``ChatMessageModel``).
"""

from __future__ import annotations

from rhizome.app.model import ViewModelBase


class WelcomeMessageModel(ViewModelBase):

    def __init__(self, user_name: str | None = None) -> None:
        super().__init__()
        self.user_name = user_name

    @property
    def greeting(self) -> str:
        return f"Welcome back, {self.user_name}" if self.user_name else "Welcome to Rhizome"
