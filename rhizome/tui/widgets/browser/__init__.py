"""Browser widget (MVVM) — multi-tab data browser with a multi-select topic tree."""

from .knowledge_entry_tab import (
    DEFAULT_PAGE_LIMIT,
    EntryTab,
    EntryTabVM,
)
from .tab_base import BrowserTabVM
from .topic_tree import TopicTree, TopicTreeVM
from .view import Browser
from .view_model import BrowserVM

__all__ = [
    "BrowserTabVM",
    "TopicTree",
    "TopicTreeVM",
    "Browser",
    "BrowserVM",
    "DEFAULT_PAGE_LIMIT",
    "EntryTab",
    "EntryTabVM",
]
