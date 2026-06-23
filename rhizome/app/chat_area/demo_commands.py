"""Demo / exercise slash commands for the chat area.

The ``/test-*`` family spawns synthetic interrupts and proposal widgets with canned data — eyeball tests
for the feed's interrupt routing and the proposal/review surfaces, with no agent involved. They live here
rather than in ``ChatAreaModel`` so the core VM stays free of demo scaffolding; ``register_demo_commands``
attaches them to an area's command registry.

``ChatAreaModel`` calls ``register_demo_commands`` only under the app's --debug flag (threaded in from
``AppConfigService`` via the workspace), so these stay out of a normal run.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rhizome.app.commands import DefaultParser, Flag
from rhizome.app.chat_pane.interrupts.commit_proposal import CommitProposalInterruptModel
from rhizome.app.chat_pane.interrupts.flashcard_proposal import FlashcardProposalInterruptModel
from rhizome.app.chat_pane.interrupts.flashcard_review import FlashcardReviewInterruptModel
from rhizome.app.chat_pane.interrupts.multi_choices import MultiUserChoicesModel
from rhizome.app.chat_pane.interrupts.sql import SqlConfirmationModel
from rhizome.app.chat_pane.interrupts.test import TestInterruptModel
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_pane.interrupts.warning import WarningUserChoicesModel
from rhizome.app.commit_proposal.commit_proposal import Entry, EntryType
from rhizome.app.flashcard_proposal import Flashcard
from rhizome.db import Topic
from rhizome.tui.types import Role

if TYPE_CHECKING:
    from .chat_area import ChatAreaModel


def register_demo_commands(area: "ChatAreaModel") -> None:
    """Register the ``/test-*`` exercise commands on ``area``'s command registry."""
    reg = area.commands

    def _report(content: str) -> None:
        area.append_message(content, Role.SYSTEM, to_agent=False)

    async def _test_interrupt() -> None:
        interrupt = TestInterruptModel(prompt="Pick an option:", options=["alpha", "beta", "gamma"])
        result = await area.present_interrupt(interrupt)
        _report("interrupt cancelled" if result is None else f"interrupt resolved: {result!r}")

    async def _test_choices() -> None:
        interrupt = UserChoicesModel.from_interrupt({
            "message": "Which fruit do you prefer?",
            "options": ["Apple", "Banana", "Cherry", "Durian"],
        })
        result = await area.present_interrupt(interrupt)
        _report("choices cancelled" if result is None else f"choices resolved: {result!r}")

    async def _test_warning_choices() -> None:
        interrupt = WarningUserChoicesModel.from_interrupt({
            "message": "The agent wants to delete 42 files from the working tree.",
            "options": ["Approve once", "Always approve in this session"],
        })
        result = await area.present_interrupt(interrupt)
        _report("warning-choices cancelled" if result is None else f"warning-choices resolved: {result!r}")

    async def _test_multiple_choices() -> None:
        interrupt = MultiUserChoicesModel.from_interrupt({
            "questions": [
                {"name": "Theme", "prompt": "Which theme should the app use?",
                 "options": ["Light", "Dark", "Solarized"]},
                {"name": "Editor", "prompt": "Which editor binding feels right?",
                 "options": ["vim", "emacs", "default"]},
                {"name": "Density", "prompt": "How dense should the layout be?",
                 "options": ["compact", "comfortable", "spacious"]},
            ],
        })
        result = await area.present_interrupt(interrupt)
        _report("multiple-choices cancelled" if result is None else f"multiple-choices resolved: {result!r}")

    async def _test_sql_confirmation() -> None:
        interrupt = SqlConfirmationModel.from_interrupt({
            "sql": (
                "UPDATE knowledge_entries\n"
                "SET title = 'Renamed entry'\n"
                "WHERE topic_id IN (SELECT id FROM topics WHERE name LIKE 'draft%');"
            ),
            "preview": {
                "columns": ["id", "title", "topic_id"],
                "rows": [
                    [1, "Old title one", 7],
                    [2, "Old title two", 7],
                    [3, "Something longer that should get truncated past the cell width limit", 8],
                    [4, "Old title four", 9],
                ],
            },
            "row_count": 12,
        })
        result = await area.present_interrupt(interrupt)
        _report("sql-confirmation cancelled" if result is None else f"sql-confirmation resolved: {result!r}")

    async def _test_flashcards(*, live_runtime: bool) -> None:
        from fsrs import Card

        def _starter_card(card_id: int) -> Card:
            # Default Card() lands in State.Learning, step 0, due=now — exactly what we want for manual
            # exercise of the FSRS step ladder.
            c = Card()
            c.card_id = card_id
            return c

        sample_cards = [
            {"id": 101, "question": "What is the time complexity of binary search?",
             "answer": "O(log n) — each comparison halves the remaining search space.",
             "fsrs_card": _starter_card(101)},
            {"id": 102, "question": "Explain the difference between a stack and a queue.",
             "answer": "A stack is LIFO: the most recently added element is removed first.\n\n"
                       "A queue is FIFO: the earliest added element is removed first.",
             "fsrs_card": _starter_card(102)},
            {"id": 103, "question": "What is a hash collision and how is it typically resolved?",
             "answer": "A hash collision occurs when two different keys produce the same hash value.\n\n"
                       "Common resolution strategies:\n"
                       "• Chaining — each bucket holds a linked list of entries\n"
                       "• Open addressing — probe for the next available slot",
             "fsrs_card": _starter_card(103)},
            {"id": 204, "question": "What does the CAP theorem state?",
             "answer": "A distributed system can provide at most two of:\n\n"
                       "• Consistency — every read returns the most recent write\n"
                       "• Availability — every request receives a response\n"
                       "• Partition tolerance — operates despite network partitions",
             "fsrs_card": _starter_card(204)},
            {"id": 205, "question": "What is the difference between concurrency and parallelism?",
             "answer": "Concurrency is about dealing with multiple tasks at once (structure).\n"
                       "Parallelism is about doing multiple tasks at once (execution).\n\n"
                       "Concurrency is possible on a single core via interleaving; parallelism "
                       "requires multiple cores.",
             "fsrs_card": _starter_card(205)},
        ]

        # Inert session factory — the VM holds it only for the optional commit() API, which the test
        # command never invokes.
        class _FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def commit(self, *_): return
        def _fake_session_factory(): return _FakeSession()

        # The model mints a stateless scorer session off ``runtime.new(KEY)`` and awaits
        # ``session.invoke(...)``, reading ``.structured_response`` as a ``{"results": [...]}`` dict.
        # By default a fake runtime returns canned scores (offline, deterministic) — ID 205 is omitted
        # to exercise the failure-fallback path. ``--live-runtime`` swaps in the area's real runtime to
        # exercise the genuine ``flashcard_scorer`` agent (needs an API key).
        auto_score_results = {101: 3, 102: 1, 103: 2, 204: 4}

        class _FakeScorerResult:
            def __init__(self, results_by_id: dict[int, int]):
                self.structured_response = {
                    "results": [
                        {"flashcard_id": i, "score": s, "feedback": ""}
                        for i, s in results_by_id.items()
                    ]
                }

        class _FakeScorerSession:
            def __init__(self, results_by_id: dict[int, int]):
                self._results_by_id = results_by_id
            async def invoke(self, _messages):
                await asyncio.sleep(1.5)
                return _FakeScorerResult(self._results_by_id)

        class _FakeRuntime:
            def __init__(self, results_by_id: dict[int, int]):
                self._results_by_id = results_by_id
            def new(self, _key):
                return _FakeScorerSession(self._results_by_id)

        runtime = area.runtime if live_runtime else _FakeRuntime(auto_score_results)
        interrupt = FlashcardReviewInterruptModel(
            cards=sample_cards,
            session_factory=_fake_session_factory,
            auto_score_enabled=True,
            agent_runtime=runtime,
        )
        result = await area.present_interrupt(interrupt)
        _report(
            "flashcards cancelled" if result is None
            else f"flashcards resolved: completed={result['completed']}, {len(result['cards'])} cards"
        )

    async def _test_commit_proposal(*, big: bool) -> None:
        algorithms_topic = Topic(id=1, name="Algorithms")
        distributed_topic = Topic(id=2, name="Distributed systems")
        sample_entries = [
            Entry(title="Binary search complexity",
                  content="Binary search has O(log n) time complexity — each comparison halves the "
                          "remaining search space.",
                  entry_type=EntryType.FACT, topic=algorithms_topic),
            Entry(title="Stack vs queue",
                  content="A stack is LIFO: the most recently added element is removed first.\n"
                          "A queue is FIFO: the earliest added element is removed first.",
                  entry_type=EntryType.EXPOSITION, topic=algorithms_topic),
            Entry(title="Hash collisions",
                  content="A hash collision is when two distinct keys produce the same hash. Common "
                          "resolutions: chaining (buckets hold linked lists) or open addressing (probe "
                          "for the next free slot).",
                  entry_type=EntryType.FACT, topic=None),
            Entry(title="CAP theorem",
                  content="A distributed system can provide at most two of: Consistency, "
                          "Availability, Partition tolerance.",
                  entry_type=EntryType.OVERVIEW, topic=distributed_topic),
        ]
        if big:
            sample_entries = [e.clone() for e in sample_entries for _ in range(10)]

        interrupt = CommitProposalInterruptModel(sample_entries, session_factory=area.session_factory)
        result = await area.present_interrupt(interrupt)
        if result is None or result["accepted"] is None:
            _report("commit-proposal cancelled")
        else:
            ei = result["edit_instructions"]
            _report(f"commit-proposal resolved: {len(result['accepted'])} accepted"
                    + (f" · edits: {ei!r}" if ei else ""))

    async def _test_flashcard_proposal(*, big: bool) -> None:
        algorithms_topic = Topic(id=1, name="Algorithms")
        distributed_topic = Topic(id=2, name="Distributed systems")
        sample_flashcards = [
            Flashcard(question="What is the time complexity of binary search?",
                      answer="O(log n) — each comparison halves the remaining search space.",
                      testing_notes="Accept any equivalent phrasing (logarithmic, log base 2, etc.).",
                      topic=algorithms_topic, entry_ids=[101, 102]),
            Flashcard(question="Explain the difference between a stack and a queue.",
                      answer="A stack is LIFO: the most recently added element is removed first.\n"
                             "A queue is FIFO: the earliest added element is removed first.",
                      testing_notes="Both LIFO/FIFO labels must be stated; pure 'opposite' answers fail.",
                      topic=algorithms_topic, entry_ids=[103]),
            Flashcard(question="What is a hash collision and how is it typically resolved?",
                      answer="A collision is two distinct keys hashing to the same bucket. Common "
                             "resolutions: chaining (linked lists per bucket) or open addressing (probe "
                             "for the next free slot).",
                      testing_notes="", topic=None, entry_ids=[]),
            Flashcard(question="What does the CAP theorem state?",
                      answer="A distributed system can provide at most two of: Consistency, "
                             "Availability, Partition tolerance.",
                      testing_notes="All three properties must be named.",
                      topic=distributed_topic, entry_ids=[204, 205, 206]),
        ]
        if big:
            sample_flashcards = [f.clone() for f in sample_flashcards for _ in range(10)]

        interrupt = FlashcardProposalInterruptModel(sample_flashcards, session_factory=area.session_factory)
        result = await area.present_interrupt(interrupt)
        if result is None or result["accepted"] is None:
            _report("flashcard-proposal cancelled")
        else:
            ei = result["edit_instructions"]
            _report(f"flashcard-proposal resolved: {len(result['accepted'])} accepted"
                    + (f" · edits: {ei!r}" if ei else ""))

    reg.register("test-interrupt", _test_interrupt, help="Spawn a synthetic interrupt to exercise routing.")
    reg.register("test-choices", _test_choices, help="Spawn a Choices interrupt with sample options.")
    reg.register("test-warning-choices", _test_warning_choices, help="Spawn a WarningChoices interrupt.")
    reg.register("test-multiple-choices", _test_multiple_choices,
                 help="Spawn a MultipleChoices interrupt with 3 questions.")
    reg.register("test-sql-confirmation", _test_sql_confirmation,
                 help="Spawn a SqlConfirmation interrupt with sample preview.")
    reg.register("test-flashcards", _test_flashcards,
                 help="Spawn a FlashcardReview interrupt with sample data.",
                 parser=DefaultParser(flags=[Flag("live-runtime",
                                                   help="Auto-score via the real runtime instead of canned scores.")]))
    reg.register("test-commit-proposal", _test_commit_proposal,
                 help="Spawn a CommitProposal interrupt with sample data.",
                 parser=DefaultParser(flags=[Flag("big", help="Spawn 10× the sample entries.")]))
    reg.register("test-flashcard-proposal", _test_flashcard_proposal,
                 help="Spawn a FlashcardProposal interrupt with sample data.",
                 parser=DefaultParser(flags=[Flag("big", help="Spawn 10× the sample flashcards.")]))
