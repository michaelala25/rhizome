"""``ResourceViewerModel`` — root orchestrator for the resource viewer.

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

from typing import TYPE_CHECKING, Any

from rhizome.app.model import ViewModelBase
from rhizome.resources import ResourceManager

from .linker import ResourceLinkerModel
from .loader import ResourceLoaderModel

if TYPE_CHECKING:
    from rhizome.tui.screens.new_resource import NewResourceResult


def _fmt_tokens(n: int | None) -> str:
    """Format a token count as a short human-readable string ('?', '1.2k', '3.4m')."""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class ResourceViewerModel(ViewModelBase):
    """Root VM. Owns the manager + child VMs and fans out topic changes. See module docstring."""

    def __init__(self, session_factory: Any, manager: ResourceManager | None = None) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._manager = manager or ResourceManager(session_factory=session_factory)

        self._current_topic_id: int | None = None
        # Display-only label for the active topic — children scope by id, but the view shows the name.
        self._current_topic_name: str | None = None

        self._loader = ResourceLoaderModel(session_factory, self._manager)
        self._linker = ResourceLinkerModel(session_factory)

        # Cross-child coupling: a committed link change reshapes the loader's resource set.
        self._linker.subscribe(self._linker.Callbacks.OnLinkChanged, self._on_link_changed)

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
    def loader(self) -> ResourceLoaderModel:
        return self._loader

    @property
    def linker(self) -> ResourceLinkerModel:
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
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Cross-child coupling
    # ------------------------------------------------------------------

    def _on_link_changed(self) -> None:
        """The linker committed link/unlink changes — refetch the loader's topic resources, since
        linking/unlinking changes which resources the loader tree shows."""
        self._loader.reload()

    # ------------------------------------------------------------------
    # Resource creation
    # ------------------------------------------------------------------

    async def create_resource(self, result: "NewResourceResult") -> str:
        """Ingest a new resource from the new-resource modal's result and reload the loader tree.

        Extracts the file text, auto-generates a title/summary via the metadata LLM (falling back to
        the filename stem if that fails), then ``ingest_resource``s it — linking to the modal's chosen
        topics, or the active topic when none were picked. Returns a human-readable status line for the
        view to surface; raises on hard failures (unreadable file, empty text, ingest error) so the
        view can report them as errors.
        """
        # Deferred: keep the resource-ingestion stack (LLM client, ingest pipeline) out of the import
        # path for callers that never create resources.
        from langchain.chat_models import init_chat_model

        from rhizome.agent.config import get_api_key
        from rhizome.resources.auto_metadata import generate_resource_metadata
        from rhizome.resources.ingest import extract_text_from_file, ingest_resource

        raw_text = extract_text_from_file(str(result.path))
        if not raw_text.strip():
            raise ValueError("No text content extracted from file.")

        # Auto-generate title + summary; tolerate LLM failures by falling back to the filename stem.
        name = result.name
        summary: str | None = None
        metadata_tokens: int | None = None
        try:
            llm = init_chat_model("claude-haiku-4-5-20251001", api_key=get_api_key(), temperature=0.0)
            meta = await generate_resource_metadata(llm, raw_text)
            summary = meta.metadata.summary
            metadata_tokens = meta.total_tokens
            if name is None:
                name = meta.metadata.title
        except Exception:
            if name is None:
                name = result.path.stem

        # Default to the active topic when the modal left the topic selection empty.
        topic_ids: list[int] = list(result.topic_ids)
        if not topic_ids and self._current_topic_id is not None:
            topic_ids.append(self._current_topic_id)

        # Carry the original bytes for formats that support later subsection extraction.
        source_type = result.path.suffix.lstrip(".").lower() or None
        try:
            source_bytes = result.path.read_bytes()
        except Exception:
            source_bytes = None

        resource_id, estimated_tokens = await ingest_resource(
            self._session_factory,
            name=name,
            raw_text=raw_text,
            topic_ids=topic_ids or None,
            loading_preference=result.loading_preference,
            summary=summary,
            source_type=source_type,
            source_bytes=source_bytes,
        )

        # Linking to the active topic reshapes the loader's resource set — refetch so the new resource
        # shows in the tree.
        self._loader.reload()

        parts = [
            f"Resource [{resource_id}] '{name}' created "
            f"(~{_fmt_tokens(estimated_tokens)} tokens, pref={result.loading_preference.value})"
        ]
        if topic_ids:
            parts.append(f"Linked to topic(s): {', '.join(str(t) for t in topic_ids)}")
        if metadata_tokens is not None:
            parts.append(f"(~{_fmt_tokens(metadata_tokens)} tokens used in generating summary)")
        return ".  ".join(parts) + "."
