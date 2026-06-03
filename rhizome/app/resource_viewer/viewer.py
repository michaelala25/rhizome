"""``ResourceViewerVM`` — root orchestrator for the resource viewer.

Thin by design. Owns the shared :class:`ResourceManager` and the three child VMs, fans a topic
change out to them, and mediates the one genuine cross-child coupling: the linker links/unlinks
resources *to the current topic*, and the loader shows *resources for the current topic*, so a
committed link change must refetch the loader. That coupling is wired here as a VM→VM hop — the
loader subscribes to the linker's ``LINK_CHANGED`` group.

The preview has no VM in this layer: it shows only metadata already carried on the highlighted
``cursor_target`` (no content fetch), so it's a pure view with no business logic to host. Which
surface drives it depends on focus, a view-side concern — the orchestrator view watches the
loader/linker ``cursor_changed`` groups and renders the focused surface's ``cursor_target`` directly.
"""

from __future__ import annotations

from typing import Any

from rhizome.app.vm import ViewModelBase
from rhizome.resources import ResourceManager

from .linker import ResourceLinkerVM
from .loader import ResourceLoaderVM


class ResourceViewerVM(ViewModelBase):
    """Root VM. Owns the manager + child VMs and fans out topic changes. See module docstring."""

    def __init__(self, session_factory: Any, manager: ResourceManager | None = None) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._manager = manager or ResourceManager(session_factory=session_factory)

        self._current_topic_id: int | None = None
        # Display-only label for the active topic — children scope by id, but the view shows the name.
        self._current_topic_name: str | None = None

        self._loader = ResourceLoaderVM(session_factory, self._manager)
        self._linker = ResourceLinkerVM(session_factory)

        # Cross-child coupling: a committed link change reshapes the loader's resource set.
        self._linker.subscribe(self._linker.link_changed, self._on_link_changed)

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def session_factory(self) -> Any:
        """The session factory backing this panel's fetches. Exposed so the view can spawn
        session-aware modals (the topic selector) against the same factory the VM uses."""
        return self._session_factory

    @property
    def manager(self) -> ResourceManager:
        return self._manager

    @property
    def loader(self) -> ResourceLoaderVM:
        return self._loader

    @property
    def linker(self) -> ResourceLinkerVM:
        return self._linker

    @property
    def current_topic_id(self) -> int | None:
        return self._current_topic_id

    @property
    def current_topic_name(self) -> str | None:
        """The active topic's display name, or ``None`` when no topic / the caller omitted it."""
        return self._current_topic_name

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_topic(self, topic_id: int | None, topic_name: str | None = None) -> None:
        """Point the panel at a topic (by id) and fan the change out to the loader and linker.
        Identity-guarded. The id is all the children need; ``topic_name`` is display-only (the view
        shows it), so callers that have it — the topic selector — pass it through."""
        if topic_id == self._current_topic_id:
            return
        self._current_topic_id = topic_id
        self._current_topic_name = topic_name
        self._loader.set_topic(topic_id)
        self._linker.set_topic(topic_id)
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Cross-child coupling
    # ------------------------------------------------------------------

    def _on_link_changed(self) -> None:
        """The linker committed link/unlink changes — refetch the loader's topic resources, since
        linking/unlinking changes which resources the loader tree shows."""
        self._loader.reload()
