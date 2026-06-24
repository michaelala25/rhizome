"""CommitProposalInterruptModel — adapter giving ``CommitProposalModel`` the chat-pane interrupt
surface (future-based resolution) and translating to/from the agent's commit-proposal contract.

Subclasses both ``CommitProposalModel`` (the core editing state machine) and ``InterruptModelBase``
(future plumbing). Cooperative ``super().__init__()`` chains ensure ``ViewModelBase`` initialises once.

The agent and the widget speak different entry shapes, and this adapter is the seam:

- **Inbound** (``from_interrupt``): the agent's ``interrupt`` value carries ``entries`` as dicts
  (``{id, title, content, entry_type, topic_id}``) plus a ``topic_map`` (id → display name). We build
  ``Entry`` objects, reconstructing a detached display ``Topic`` per entry, and remember each entry's
  stable ``id`` by position (``Entry`` itself has no id — the widget addresses by index).

- **Outbound** (``_build_result``): the widget resolves into a JSON-serialisable dict the agent reads
  back — ``Entry``/``Topic`` objects can't cross the langgraph interrupt boundary, so the kept entries
  are serialised back to the proposal dict shape, with their original ids re-attached so the agent can
  diff the result against what it proposed.

Resolution model: the VM auto-resolves its future when the lifecycle reaches ``DONE``. ``accept()``
resolves with the kept entries (no feedback); ``submit_revision(text)`` resolves with the kept entries
plus the feedback; ``cancel()`` resolves with ``accepted=None``. Cancel does *not* cancel the underlying
future — we resolve with a typed payload so the agent distinguishes approve / revise / cancel by
inspecting the result rather than catching ``CancelledError``.

Result shape::

    {
        "accepted": list[dict] | None,   # kept entries {id, title, content, entry_type, topic_id};
                                         # None iff cancelled
        "edit_instructions": str,        # the revision feedback iff the user asked to revise, else ""
    }
"""

from __future__ import annotations

from typing import Any

from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel, Entry
from rhizome.db import Topic


class CommitProposalInterruptModel(CommitProposalModel, InterruptModelBase):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Interrupts are interactive feed entries — opt into the chat pane's ctrl+up/ctrl+down rotation.
        self.is_navigable = True
        # The agent's per-entry ids, by position — re-attached on resolve so the agent can diff the
        # result. None when constructed directly (the demo), where positions stand in for ids.
        self._source_ids: list[int] | None = None
        # ``OnDone`` is fire-once on the EDITING → DONE transition; resolve the future there.
        self.subscribe(self.Callbacks.OnDone, self._on_done)

    @classmethod
    def from_interrupt(cls, value: dict[str, Any], context: Any) -> "CommitProposalInterruptModel":
        """Build the proposal surface from the agent's interrupt value. The DB session factory rides in
        on the run context, powering the in-widget topic-reassignment browser."""
        topic_map = value.get("topic_map") or {}
        entries: list[Entry] = []
        source_ids: list[int] = []
        for d in value.get("entries", []):
            entries.append(Entry.from_dict(d, topic=_display_topic(d.get("topic_id"), topic_map)))
            source_ids.append(d.get("id"))
        vm = cls(entries, session_factory=getattr(context, "session_factory", None))
        vm._source_ids = source_ids
        return vm

    def _on_done(self, outcome: CommitProposalModel.Outcome) -> None:
        if self.resolved:
            return
        self.resolve(self._build_result(), remain_navigable=True)

    def _build_result(self) -> dict[str, Any]:
        if self.cancelled:
            return {"accepted": None, "edit_instructions": ""}
        kept = [(i, e) for i, e in enumerate(self.entries) if i not in self.excluded]
        return {
            "accepted": [self._serialize(i, e) for i, e in kept],
            "edit_instructions": self.revision_feedback or "",
        }

    def _serialize(self, index: int, entry: Entry) -> dict[str, Any]:
        entry_id = self._source_ids[index] if self._source_ids is not None else index
        return {
            "id": entry_id,
            "title": entry.title,
            "content": entry.content,
            "entry_type": entry.entry_type.value,
            "topic_id": entry.topic.id if entry.topic is not None else None,
        }


def _display_topic(topic_id: Any, topic_map: dict) -> Topic | None:
    """A detached display ``Topic`` (id + name) for the proposal surface. ``topic_map`` keys may arrive
    int- or string-typed depending on the checkpoint serialiser, so look up both."""
    if topic_id is None:
        return None
    name = topic_map.get(topic_id) or topic_map.get(str(topic_id)) or ""
    return Topic(id=topic_id, name=name)
