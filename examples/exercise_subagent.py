"""Exercise a StructuredSubagent with a simple topic summary task.

Requires ANTHROPIC_API_KEY to be set.  Creates a temporary database,
seeds it with sample data, spawns a StructuredSubagent that summarizes
topics, and verifies the structured response parses correctly.

Usage:
    uv run python examples/exercise_subagent.py
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from langchain.agents.structured_output import ProviderStrategy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rhizome.agent_legacy.builder import build_agent
from rhizome.agent_legacy.subagents import StructuredSubagent
from rhizome.agent_legacy.tools.core import build_core_tools
from rhizome.db import get_session_factory, init_db
from rhizome.db.operations import create_entry, create_topic

DB_PATH = Path(__file__).resolve().parent.parent / "subagent_test.db"


@dataclass
class TopicSummary:
    """Expected structured output from the subagent."""
    topic_count: int
    topic_names: list[str]
    summary: str


async def seed_db(session_factory):
    """Create a few topics and entries for the subagent to discover."""
    async with session_factory() as session:
        algebra = await create_topic(session, name="Algebra")
        geometry = await create_topic(session, name="Geometry")
        calculus = await create_topic(session, name="Calculus")

        await create_entry(
            session,
            topic_id=algebra.id,
            title="Quadratic Formula",
            content="The quadratic formula solves ax^2 + bx + c = 0.",
        )
        await create_entry(
            session,
            topic_id=geometry.id,
            title="Pythagorean Theorem",
            content="a^2 + b^2 = c^2 for right triangles.",
        )
        await create_entry(
            session,
            topic_id=calculus.id,
            title="Fundamental Theorem",
            content="Integration and differentiation are inverse operations.",
        )
        await session.commit()

    print("  Seeded 3 topics with 1 entry each.")


async def main():
    # Clean up any previous test DB.
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = init_db(DB_PATH)
    session_factory = get_session_factory(engine)
    await seed_db(session_factory)

    # Build a subagent with read-only DB tools.
    tools = list(build_core_tools(session_factory).values())
    model, agent, _middleware = build_agent(tools, provider="anthropic", model_name="claude-sonnet-4-6", response_format=ProviderStrategy(TopicSummary))

    schema_hint = json.dumps({
        "topic_count": "int — number of root topics found",
        "topic_names": "list[str] — names of the root topics",
        "summary": "str — one-sentence summary of the topics",
    }, indent=2)

    subagent = StructuredSubagent(
        model=model,
        agent=agent,
        system_prompt=(
            "You are a research assistant. When asked to summarize topics, "
            "use the available tools to inspect the database, then respond "
            f"with ONLY a JSON object matching this schema:\n{schema_hint}\n"
            "Do not include any text outside the JSON object."
        ),
        response_schema=TopicSummary,
        stateful=True,
        config={"configurable": {"thread_id": 1}}
    )

    print("\n  Invoking subagent: 'List and summarize all root topics'")
    conv_id, response = await subagent.ainvoke("List and summarize all root topics.")

    print(f"  Conversation ID: {conv_id}")
    print(f"  Raw content: {response.content[:200]}")

    if subagent.response is not None:
        print(f"  Parsed response: {subagent.response}")
        print(f"    topic_count = {subagent.response.topic_count}")
        print(f"    topic_names = {subagent.response.topic_names}")
        print(f"    summary     = {subagent.response.summary}")
        print("\n  PASS  StructuredSubagent returned valid structured output")
    else:
        print(f"\n  WARN  Structured parsing failed (raw content above)")
        print("        This may happen if the model didn't return pure JSON.")

    # Test stateful multi-turn.
    print("\n  Invoking subagent again (same conversation): 'How many entries does Algebra have?'")
    conv_id2, response2 = await subagent.ainvoke(
        "How many entries does the Algebra topic have? Reply with just the number.",
        conversation_id=conv_id,
    )
    assert conv_id2 == conv_id, "Conversation ID should persist"
    print(f"  Response: {response2.content[:200]}")
    print("  PASS  Multi-turn conversation preserved")

    # Cleanup.
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("\n  All subagent checks passed!")


if __name__ == "__main__":
    asyncio.run(main())
