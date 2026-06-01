"""View models for the ResourceViewer widget family.

These classes hold the persistent state that survives widget destroy/recreate
cycles (e.g. when changing dock position).  Each widget reads from its view
model on compose/mount and writes back on user interaction.

Hierarchy::

    ResourceViewerViewModel
    ├── .resource_list   → ResourceListViewModel
    ├── .resource_linker → ResourceLinkerViewModel
    └── .resource_loader → ResourceLoaderViewModel
"""

from __future__ import annotations

import enum

from rhizome.db import Resource, Topic
from rhizome.resources import ResourceLoadType, ResourceTreeNodeKey


# ======================================================================
# Sub-widget view models
# ======================================================================

class ResourceListViewModel:
    """State for the ResourceList widget."""

    def __init__(self) -> None:
        self.resources: list[Resource] = []
        self.cursor: int = 0
        self.show_ids: bool = False


class ResourceLinkerViewModel:
    """State for the ResourceLinker widget."""

    def __init__(self) -> None:
        self.resources: list[Resource] = []
        self.linked_ids: set[int] = set()
        self.cursor: int = 0
        self.show_ids: bool = False


class ResourceLoaderViewModel:
    """State for the ResourceLoader widget.

    ``states`` is an MDL-form dict: an entry at ``(kind, id)`` means that
    node and every descendant are loaded at the given :class:`ResourceLoadType`,
    unless a descendant has its own overriding entry.  Absence = unloaded.

    ``pending_resources`` holds resource ids currently computing embeddings;
    pending resources are rendered with a spinner, are locked against
    further toggles, and are filtered out when syncing state to the manager.
    """

    def __init__(self) -> None:
        self.resources: list[Resource] = []
        self.states: dict[ResourceTreeNodeKey, ResourceLoadType] = {}
        self.pending_resources: set[int] = set()
        self.show_ids: bool = False
        self.spinner_frame: int = 0


# ======================================================================
# Top-level view model
# ======================================================================


class ResourceViewMode(enum.IntEnum):
    """Which resource view is active in the ResourceViewer panel."""
    TOPIC_RESOURCES = 0
    LINK_RESOURCES = 1
    LOAD_RESOURCES = 2


class ResourceViewerViewModel:
    """Persistent state for the ResourceViewer panel.

    Created once by ChatPane and handed to each new ResourceViewer
    instance.  Survives widget destruction so caches, active topic,
    and load states are preserved across dock-position changes.
    """

    def __init__(self) -> None:
        # -- View mode & display ------------------------------------------
        self.view_mode: ResourceViewMode = ResourceViewMode.TOPIC_RESOURCES
        self.show_ids: bool = False

        # -- Active / cursor topic ----------------------------------------
        self.active_topic: Topic | None = None
        self.active_topic_path: list[str] = []
        self.current_topic_id: int | None = None

        # -- Caches -------------------------------------------------------
        self.resource_cache: dict[int, list[Resource]] = {}
        self.loader_resource_cache: dict[int, list[Resource]] = {}
        self.resource_cursor_cache: dict[int, int] = {}
        self.all_resources: list[Resource] | None = None
        self.linked_ids_cache: dict[int, set[int]] = {}

        # -- Composed sub-view-models -------------------------------------
        self.resource_list = ResourceListViewModel()
        self.resource_linker = ResourceLinkerViewModel()
        self.resource_loader = ResourceLoaderViewModel()
