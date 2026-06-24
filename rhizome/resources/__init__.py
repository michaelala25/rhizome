"""Resource loading layer: the content-free skeleton (``ResourceTree``), load-state descriptions and
their arithmetic, the per-channel stores (context stuffing + vector index), and content building."""

from .content import build_index_block, build_resource_block
from .index import ResourceVectorStore
from .store import (
    aggregate,
    close_upward,
    expand,
    load_delta,
    normalize,
    ResourceContextStore,
    ResourceIndexStore,
    ResourceLoadDelta,
    ResourceStore,
)
from .tree import ResourceTree, ResourceTreeNode

__all__ = [
    "aggregate",
    "build_index_block",
    "build_resource_block",
    "close_upward",
    "expand",
    "load_delta",
    "normalize",
    "ResourceContextStore",
    "ResourceIndexStore",
    "ResourceLoadDelta",
    "ResourceStore",
    "ResourceTree",
    "ResourceTreeNode",
    "ResourceVectorStore",
]
