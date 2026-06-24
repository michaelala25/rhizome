"""Commit-proposal interrupt round-trip — the agent ↔ widget contract.

The agent sends entries as dicts (``{id, title, content, entry_type, topic_id}``) plus a ``topic_map``;
the widget edits ``Entry`` objects (``Topic``-valued, no id); the result must come back JSON-serialisable
in the agent's dict shape with ids re-attached. These pin that translation in both directions, plus the
router routing the ``commit_proposal`` interrupt type to the adapter.

Tests are async: the interrupt VM allocates an ``asyncio.Future`` at construction, which needs a loop.
"""

from types import SimpleNamespace

from rhizome.agent.state import CommitProposalEntry
from rhizome.app.chat_area.stream_router import ChatAreaStreamRouter
from rhizome.app.chat_pane.interrupts.commit_proposal import CommitProposalInterruptModel


def _commit_interrupt_value() -> dict:
    return {
        "type": "commit_proposal",
        "entries": [
            {"id": 0, "title": "Binary search", "content": "O(log n).", "entry_type": "fact", "topic_id": 1},
            {"id": 1, "title": "CAP", "content": "Pick two of three.", "entry_type": "overview", "topic_id": 2},
        ],
        "topic_map": {1: "Algorithms", 2: "Distributed systems"},
    }


def _ctx(session_factory=None):
    return SimpleNamespace(session_factory=session_factory)


async def test_commit_proposal_from_interrupt_builds_entries_topics_and_tracks_ids():
    vm = CommitProposalInterruptModel.from_interrupt(_commit_interrupt_value(), _ctx())
    assert [e.title for e in vm.entries] == ["Binary search", "CAP"]
    assert [e.entry_type.value for e in vm.entries] == ["fact", "overview"]
    assert [(e.topic.id, e.topic.name) for e in vm.entries] == [(1, "Algorithms"), (2, "Distributed systems")]
    assert vm._source_ids == [0, 1]                       # ids tracked by position (Entry has none)


async def test_commit_proposal_accept_serializes_back_to_the_agent_shape():
    vm = CommitProposalInterruptModel.from_interrupt(_commit_interrupt_value(), _ctx())
    vm.accept()

    assert vm.result["edit_instructions"] == ""
    assert vm.result["accepted"] == [
        {"id": 0, "title": "Binary search", "content": "O(log n).", "entry_type": "fact", "topic_id": 1},
        {"id": 1, "title": "CAP", "content": "Pick two of three.", "entry_type": "overview", "topic_id": 2},
    ]
    # Exactly the shape the tool reconstructs CommitProposalEntry from — proves the contract lines up.
    rebuilt = [CommitProposalEntry(**e) for e in vm.result["accepted"]]
    assert rebuilt[0]["id"] == 0 and rebuilt[0]["topic_id"] == 1


async def test_commit_proposal_exclude_revise_and_cancel():
    # Exclude entry 1 then accept → only entry 0 survives; the agent diffs the missing id as excluded.
    vm = CommitProposalInterruptModel.from_interrupt(_commit_interrupt_value(), _ctx())
    vm.set_excluded(1, True)
    vm.accept()
    assert [e["id"] for e in vm.result["accepted"]] == [0]

    # Inline edit + request revision with feedback → accepted carries the edit, edit_instructions set.
    vm2 = CommitProposalInterruptModel.from_interrupt(_commit_interrupt_value(), _ctx())
    vm2.set_entry_title(0, "Binary search (edited)")
    vm2.request_revision()
    vm2.submit_revision("tighten the wording")
    assert vm2.result["edit_instructions"] == "tighten the wording"
    assert vm2.result["accepted"][0]["title"] == "Binary search (edited)"

    # Cancel → accepted is None (the agent reads cancel from this).
    vm3 = CommitProposalInterruptModel.from_interrupt(_commit_interrupt_value(), _ctx())
    vm3.cancel()
    assert vm3.result["accepted"] is None


async def test_commit_proposal_routes_through_the_stream_router():
    vm = ChatAreaStreamRouter._build_interrupt_vm(_commit_interrupt_value(), _ctx())
    assert isinstance(vm, CommitProposalInterruptModel)
    assert [e.title for e in vm.entries] == ["Binary search", "CAP"]


async def test_commit_proposal_topic_map_tolerates_string_keys():
    # A checkpoint serialiser may stringify the topic_map's int keys; the display lookup must still hit.
    value = _commit_interrupt_value()
    value["topic_map"] = {"1": "Algorithms", "2": "Distributed systems"}
    vm = CommitProposalInterruptModel.from_interrupt(value, _ctx())
    assert [(e.topic.id, e.topic.name) for e in vm.entries] == [(1, "Algorithms"), (2, "Distributed systems")]
