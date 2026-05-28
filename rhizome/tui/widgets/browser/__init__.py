"""Browser widget (MVVM) — multi-tab data browser with a multi-select topic tree.

VMs live under ``rhizome.app.browser``; views under ``rhizome.tui.widgets.browser``. This package
re-exports the top-level entry points (``Browser`` view, ``BrowserVM``) plus the entries-tab
defaults that the screen layer reads at construction time.
"""

from rhizome.app.browser.browser import BrowserVM
from rhizome.app.browser.tab_base import BrowserTabVM
from rhizome.app.browser.tabs.entries.tab import DEFAULT_PAGE_LIMIT, EntryTabVM
from rhizome.app.browser.topics.tree import TopicTreeVM

from .browser import Browser
from .tabs.entries.tab import EntryTab
from .topics.tree import TopicTree

__all__ = [
    "Browser",
    "BrowserTabVM",
    "BrowserVM",
    "DEFAULT_PAGE_LIMIT",
    "EntryTab",
    "EntryTabVM",
    "TopicTree",
    "TopicTreeVM",
]
