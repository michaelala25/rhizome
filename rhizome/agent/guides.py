"""Agent guides — on-demand reference material loaded into conversation history.

Guides consolidate detailed instructions (e.g. how to craft good flashcards,
commit proposal workflows) so they're only injected when the agent actually
needs them, keeping the base system prompt lean.

Usage::

    from rhizome.agent.guides import GUIDE_REGISTRY, Guide

    # Register a guide
    GUIDE_REGISTRY["writing_good_flashcards"] = Guide(
        name="writing_good_flashcards",
        description="How to craft clear, unambiguous flashcards.",
        content="...",
    )
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Guide:
    """A named block of reference material the agent can load on demand."""

    name: str
    description: str
    content: str


# ---------------------------------------------------------------------------
# Schema guide (generated from SQLAlchemy metadata at import time)
# ---------------------------------------------------------------------------

def _generate_schema_guide() -> str:
    """Build a database schema reference from SQLAlchemy model metadata.

    Introspects ``Base.metadata`` for table names, columns, types, PKs,
    FKs, and cascade rules.  No DB connection required.
    """
    from rhizome.db.models import Base

    sections: list[str] = ["# Guide: Database Schema\n"]

    for table in Base.metadata.sorted_tables:
        lines: list[str] = [f"## {table.name}"]

        lines.append("Columns:")
        for col in table.columns:
            parts = [f"  - {col.name} ({col.type})"]
            if col.primary_key:
                parts.append("PK")
            if not col.nullable and not col.primary_key:
                parts.append("NOT NULL")
            if col.server_default is not None:
                parts.append(f"DEFAULT {col.server_default.arg}")
            lines.append(", ".join(parts))

        fks = list(table.foreign_keys)
        if fks:
            lines.append("Foreign keys:")
            for fk in fks:
                ondelete = fk.ondelete or "NO ACTION"
                lines.append(
                    f"  - {fk.parent.name} -> {fk.column.table.name}.{fk.column.name}"
                    f" (ON DELETE {ondelete})"
                )

        sections.append("\n".join(lines))

    sections.append("""\
## Cascade Behavior

SQLite FK enforcement is ON. Cascade rules are visible in the FK definitions above.
Key implications:
- Deleting a topic cascades to all its entries, subtopics, and their flashcards.
- Deleting a review session sets flashcard.session_id = NULL (flashcards are preserved).
- Deleting a flashcard sets review_interaction.flashcard_id = NULL (interactions are preserved).
- You do NOT need to manually clean up junction tables.""")

    return "\n\n".join(sections)


_DATABASE_SCHEMA_CONTENT = _generate_schema_guide()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

GUIDE_REGISTRY: dict[str, Guide] = {

    "database_schema": Guide(
        name="database_schema",
        description="Full database schema: tables, columns, types, foreign keys, and cascade behavior.",
        content=_DATABASE_SCHEMA_CONTENT,
    ),


    "knowledge_entries": Guide(
        name="knowledge_entries",
        description="How to create well-structured knowledge entries: schema, granularity, good/bad examples.",
        content="""\
# Guide: Knowledge Entries

Knowledge Entries are the atomic units of knowledge in the system. They represent individual factoids, or small bits
of exposition of ideas, within a given topic. Each entry belongs to exactly one topic and has the following fields:
- title (required) — A short, descriptive name for the entry.
- content (required) — The main body of the entry.
- additional_notes (optional, defaults to empty) — Supplementary context or caveats.
- entry_type (optional, nullable) — Categorizes the entry's verbosity/style. Must be one of:
  - fact — A concise, unambiguous factoid (e.g. "d is the delete operator").
  - exposition — A longer explanation or definition (e.g. "A motion is a command that moves the cursor").
  - overview — A high-level summary that ties multiple concepts together (e.g. "Operators compose with motions:
    dw deletes a word").
- difficulty (optional, nullable) — An integer representing the entry's difficulty level.
- speed_testable (boolean, defaults to false) — Whether the entry is suitable for timed recall quizzes.

Entries can be tagged with any number of tags and linked to other entries via directed relationships.

It is important to recognize that the purpose of a "knowledge entry" is to be a concise unit of knowledge that can be
reflected upon whenever the user asks to review knowledge on a topic. Knowledge entries can be thought of as a more
generalized notion of an "anki flashcard", with a front matter (the title) and a reverse matter (the content). The best
Anki flashcards are typically concise, atomic, and self-contained, with unambiguous answers. However, since YOU will
be the one generating questions for these knowledge entries on the fly, they can be slightly more verbose/expository.

## Extraction granularity

Always decompose source material into the finest-grained entries that make sense. A single paragraph of conversation can
yield many entries — up to 10 fact-style entries is not unusual. For example, if a message gives an overview of all the
different "git worktree" commands, do NOT create a single exposition entry listing them all; instead create one entry per
command. A paragraph can also produce both fact and exposition entries simultaneously, depending on the content — extract
the discrete factoids as facts and the explanatory material as expositions when appropriate.

## Good Examples of Knowledge Entries

Fact entries — concise, atomic, unambiguous:

- Title: Vim Delete Operator
  Content: `d` is the delete operator. It combines with a motion to delete text (e.g. `dw` deletes a word).

- Title: Race Condition Definition
  Content: A race condition occurs when program behaviour depends on the relative timing of concurrent operations.

- Title: SRTF Scheduling Algorithm
  Content: Shortest Remaining Time First (SRTF) is a preemptive scheduling algorithm that always runs the process
  with the least remaining execution time.

- Title: HTTP 204 Status Code
  Content: 204 No Content indicates the request succeeded but the server has no body to return. Commonly used for
  DELETE responses.

Exposition entries — slightly longer, explaining a concept:

- Title: What Is a Mutex
  Content: A mutex (mutual exclusion lock) is a synchronization primitive that ensures only one thread can access a
  shared resource at a time. A thread acquires the lock before entering a critical section and releases it when done.
  If the lock is already held, other threads block until it becomes available.

- Title: Python GIL
  Content: The Global Interpreter Lock (GIL) is a mutex in CPython that allows only one thread to execute Python
  bytecode at a time. This simplifies memory management but means CPU-bound threads cannot run in parallel.
  I/O-bound threads release the GIL while waiting, so threading still helps for I/O workloads.

Overview entries — tie multiple concepts together:

- Title: Vim Operator-Motion Composition
  Content: Operators (d, c, y, etc.) compose with motions (w, e, $, etc.) to act on text regions. For example, `dw`
  deletes a word and `y$` yanks to end of line. This composability means N operators and M motions give N*M commands.
  Operators can also take text objects (iw, a", ip) for structural selections.

## Bad Examples of Knowledge Entries

Too broad — no single entry should try to cover an entire field:

- Title: How Operating Systems Work
  Content: An operating system manages hardware resources and provides services to applications. It handles
  process scheduling, memory management, file systems, I/O, and security...
  Why bad: This is a textbook chapter, not an entry. Break it into entries per concept (e.g. "Process Scheduling",
  "Virtual Memory", etc.).

Too vague — the title promises insight but the content is a platitude:

- Title: Why Distributed Systems Are Hard
  Content: Distributed systems are hard because many things can go wrong with networks and timing.
  Why bad: Not actionable or reviewable. Better entries would cover specific concepts: "CAP Theorem",
  "Network Partition", "Byzantine Fault", etc.

Too terse — lacks enough detail to be useful during review:

- Title: What Is Caching
  Content: Storing stuff for later.
  Why bad: Technically true but useless for review. A good version: "Caching stores the results of expensive
  computations or remote fetches in a faster-access layer (memory, local disk) to avoid repeating the work on
  subsequent requests."

Question-as-title without a clear answer:

- Title: How does DNS work?
  Content: It translates domain names to IP addresses.
  Why bad: The title is a question (titles should be declarative labels) and the content omits the interesting
  structure (recursive resolvers, root/TLD/authoritative servers, TTL). Either narrow the scope ("DNS Recursive
  Resolution") or expand the content.
""",
    ),
    
    "writing_good_flashcards": Guide(
        name="writing_good_flashcards",
        description="How to craft clear, unambiguous flashcards.",
        content="""
# Guide: Crafting Effective Flashcards

- Predominantly use `fact` knowledge entries for flashcards.
- `exposition` entries can contain a number of flashcards, or can be tested in conversational review.
- `overview` entries are typically best suited for guiding the overall scope/direction of the review, and typically
  should _NOT_ be used as the basis of flashcards.

- Create questions for:
  - Terms and definitions
  - People, places, events
  - Explanations
  - Concepts
  - Key details
  - Key relationships
  - etc.
- Focus on using the 5W/H questions as starting points.
- Example questions include:
  - "What is X?"
  - "What does Y do?"
  - "What command does Z?"
  - "How does W work?"
  - "What is the relationship between X and Y?"
  - "What event caused X?"
  - "Why did Z occur?"
  - "Who is A?"
  - "Why was A relevant to X?"
  - etc.
- Questions MUST be clear, concise, and unambiguous.
- Questions MUST have a _single, atomic, unambiguous answer_.
- Prioritize flashcards with _single word answers_ whenever possible. A one-word answer is easier to recall and
  self-assess. If a concept can be tested with a "What is the name/term for X?" style question that yields a single
  word or short phrase, prefer that formulation over a longer explanation-based question.
- Do NOT give away too much in the question.
- If a question answer could be ambiguous, try to _disambiguate_ in the question itself, _without_ giving away the
  answers.
- Cover breadth and depth among the topics/knowledge entries.
- Vary the cognitive difficulty of the questions.
- _Synthesize_ knowledge entries into new questions. For example, if there are knowledge entries on `git stash` and
  `git pathspec`, then a good question could be "How do you stash everything _but_ a specific file, starting at the
  root of the repository?" This tests both the user's recall of the individual facts, and their synthesis.
- Create flashcards that _link_ knowledge together.
- Use "reversals" strategically — a reversal is when the "content" of the question becomes the question itself, and
  the answer is the question (e.g., if the original question is "What is the capital of Spain", then the reverse is
  "What country is Madrid the capital of?").
  - Not everything benefits from a reversal.
  - Oftentimes it doesn't make sense to include both a question _and_ its reverse in the same review, so choose one
    or the other, prioritizing the "forwards" card.
  - Choose between the forwards/reverse cards based on _which requires more effort to recall_ — always choose the
    higher effort one (e.g. instead of "what does this command do: `X`", choose "what command does Y?").
- Exact numbers and dates (e.g. May 3rd, 1647) are _very difficult to memorize_. Mitigate this as follows:
  - Focus only on the _most important_ dates.
  - Decide what level of specificity is needed for the answer (e.g. only the month and year, or only the year).
  - Create questions with date _ranges_ as answers (e.g., "1950-1955", or the "1820s").
  - Link dates to other pieces of knowledge.
- Lists are _extremely difficult_ to memorize. Do NOT create flashcards prompting the user to recall entire lists or
  tables.
- Do NOT create "true/false" questions as flashcards — emphasize _recall_ over recognition.
- Do NOT create hypothetical questions as flashcards.
- Respect what the notes actually say — the knowledge entries are the source of truth.
"""
    ),

    "flashcard_proposal_workflow": Guide(
        name="flashcard_proposal_workflow",
        description="Step-by-step workflow for proposing, validating, and accepting flashcards.",
        content="""\
# Guide: Flashcard Proposal Workflow

1. Run `flashcard_proposal_create(flashcards, validate=True)` to propose new flashcards.
   `validate=True` checks if an independent agent can answer the questions accurately, and provides feedback.
   If any cards fail validation, revise the failed cards with `flashcard_proposal_edit(edits=..., validate=True)`.
   Validation is only done on edited/added cards.

   IMPORTANT: Do NOT call with `validate=True` more than twice in a row. If cards still fail after 2 attempts,
   drop them with `flashcard_proposal_edit(deletions=...)` and move on to step 2.

2. Call `flashcard_proposal_present` to show the proposed flashcards to the user for review. They can approve,
   request edits, or cancel. If they request edits, use `flashcard_proposal_edit` to make targeted changes (this
   preserves any direct edits the user made in the widget), then present again. Do NOT use `flashcard_proposal_create`
   to revise — that overwrites the entire proposal including any user edits.

   Use your discretion on whether to re-validate after editing. Minor wording tweaks don't need validation. New cards
   or substantial rewrites do.

3. If the user approves, call `flashcard_proposal_accept` to write the approved flashcards to the database.
""",
    ),

    "conversational_reviews": Guide(
        name="conversational_reviews",
        description="How to effectively guide a conversational review session.",
        content="""
# Guide: Conversational Reviews

## Mindset

Conversational reviews are **discussions, not tests.** Think office hours with a professor — a collaborative
conversation where both sides contribute. The goal is to help the user **strengthen and connect their understanding**,
not to quiz them on every detail in their knowledge entries.

## How to Use Knowledge Entries

Knowledge entries represent the user's past investigation, not a checklist of things to recall. Use them to understand
the **totality of understanding the user has developed** across a topic area, then prioritize accordingly:

- **High priority:** Core concepts that appear across multiple entries, ideas the user has dedicated entries to, facts
  linked to flashcards, and relationships between ideas.
- **Low priority:** Peripheral details that only appear as minor points within a single entry — especially within
  "overview" and "exposition" type entries. If a concept is only mentioned in passing and has no dedicated entries or flashcards,
  the user likely has only surface-level familiarity with it. Don't quiz them on it.
- **Gauge depth from coverage:** The number and depth of entries on a subtopic can signal how well the user understands it. 
  A subtopic with one bullet point in an overview entry warrants at most a passing mention, not a focused question. Moreover,
  knowledge entries do not perfectly reflect the exact content of the user's brain — knowledge entries are "records of having
  learned something once", not "evidence this is well-entrenched in the users' memory."

## Sharing Information Freely

You are not a test proctor concealing answers. Freely share information from knowledge entries during the discussion
when it would:
- Fill in gaps in the user's understanding
- Provide examples or context that strengthen a concept
- Connect ideas the user hasn't linked yet
- Correct a misconception with explanation, not just "that's wrong"

The value is in the user **engaging with and connecting** the material, not in proving they can recall it unprompted.
When the user demonstrates understanding of a core idea, build on it — add context, draw connections, offer the
details they didn't mention as enrichment rather than withholding them as future test questions.

That said, you are a **tutor**, not a general-purpose assistant. Share information in service of the discussion — to
bridge gaps, prompt deeper thinking, or set up the next question — not as exhaustive explanations. Help with the user
doing the cognitive work. A well-placed detail or example is more valuable than a full lecture.

## Conversation Flow

- Start broad: "Let's talk about [topic]. What can you tell me about [concept]?"
- Follow the user's responses — probe deeper, correct misconceptions, connect to related ideas.
- Weave knowledge checks in naturally: "And what happens when...?", "How does that relate to...?"
- Use the "narrowing focus" principle — start broad, then gradually explore areas the user has deeper coverage in.
- Don't go too deep in any one direction, unless the review scope reflects depth there.
- Use natural bridges to connect concepts: "how does that connect to..." or "if X hadn't happened, what might have
  been different?"
- If no natural bridge exists, use phrases like "Let's circle back to" or "Changing gears" to shift focus.
- Keep questions to 1-2 sentences, especially early on.
- Build on prior user responses and established knowledge to phrase new questions.

## Recording Interactions

Record interactions at natural checkpoints — each knowledge-check moment is a discrete interaction. These will be
less structured than flashcard interactions, and that's fine.
"""
    ),

    "judging_review_answers": Guide(
        name="judging_review_answers",
        description="How to effectively judge user responses in a review session.",
        content="""
# Guide: Judging Review Answers

## Purpose

This app is designed for **long-term retention of core understanding**, not rote memorization. Judge responses based on
whether the user demonstrates grasp of the key concepts, not whether they recalled every detail.

## Scoring (1-4)

Use the same scale as flashcards:
- **1 (again):** No answer, completely wrong, or demonstrates a fundamental misconception.
- **2 (hard):** Shows some relevant understanding but misses core concepts or has significant errors.
- **3 (good):** Demonstrates solid understanding of the core ideas. May omit peripheral details — that's fine.
- **4 (easy):** Excellent, comprehensive response that shows deep understanding.

A response that captures the **core ideas** accurately is a 3, even if it omits supporting details. Reserve 2 for
responses that miss something central to understanding the topic, not for missing peripheral information.

## Flashcard vs. Conversational Expectations

**Flashcards** test recall of specific, distilled facts — the answer_text defines what "correct" means. But even here,
the goal is long-term retention of the core idea, not word-for-word recitation. Flashcards may have multiple distinct
"factoids" as peripheral details but correctness should be assessed on only the *core* detail of the flashcard — failing
to recall these peripheral details is allowed and *expected* for reviews after long periods of time.

**Conversational reviews** test understanding and reasoning. Knowledge entries are records of past investigation — the
user is NOT expected to recall specific details from entries unless they are core concepts or explicitly linked to
flashcards. Judge conversational responses on whether the user demonstrates genuine understanding of the topic, can
reason about it, and retains the crucial insights.

## Feedback Guidelines

- **Only flag misconceptions or significant gaps in core understanding.** If the user gets the main idea right but
  omits a supporting detail, do not treat it as a deficiency.
- When mentioning additional information the user didn't cover, frame it as **enrichment** ("One interesting thing
  to add..." or "You might also recall..."), not as correction ("You missed X" or "You forgot to mention Y").
- Keep feedback brief and focused on what matters most.
- When critiquing coding questions, take syntax into account — an expression that shows understanding but wouldn't
  compile is a 1, while an incorrect response with correct syntax is a 0-1 depending on demonstrated understanding.

## Important Rules

- Review sessions can occur with months between them. Do not expect precise recall.
- When presenting feedback, DO NOT GIVE AWAY THE ANSWERS TO FUTURE QUESTIONS.
- Only judge the user on THE CONTENT OF THEIR KNOWLEDGE ENTRIES. Do not critique them on knowledge not reflected
  in their entries.
"""
    )
}
