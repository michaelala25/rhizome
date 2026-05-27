# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Rhizome is a knowledge management system designed for LLM agent integration. It supports structured learning (storing knowledge entries) and practice through review sessions.

**Tech stack:** Python 3.14+, SQLAlchemy 2.x (async), aiosqlite, SQLite, alembic

## Commands

```bash
# Install dependencies
uv sync

# Run any Python script
uv run python <script.py>
```

## CONTEXT.md Files

Each directory under `rhizome/` contains a `CONTEXT.md` describing its contents, purpose, and how it fits into the larger system. **Always read** the relevant `CONTEXT.md` files before planning changes or writing new code in a directory. When working in a _new_ directory, a `CONTEXT.md` is typically not required until specified by the user.

### CONTEXT.md Style Guide

`CONTEXT.md` files are supposed to be quick "at-a-glance" references. Describe at a high level "what you'll find in this directory" and the "how it fits into the bigger picture" (e.g., what are the seams that glue this layer to other layers, etc.).

There is NO general rule about "top-down" versus "bottom-up" writing in context files - that is, it is NOT assumed that innermost context files implicitly assume an understanding of all parent context files, or vice versa. Context files can contain arbitrary references to other parts of the codebase as needed in order to ground the discussion.

Context files can also include:
- Conventions established for a specific directory (and whether or not this assumption should be made for all subdirectories). These can include (but are not limited to):
  - Coding standards
  - Established patterns of communication/data flow
  - Design paradigms
  - Documentation protocol
  - Other major decisions
- Complex/confusing bits of behaviour
- Communication channels

If a context file is growing large (>100 lines), check if any of the following apply:
- Descriptions of classes/objects are becoming too verbose - leave docstrings/in-code documentation for more verbose descriptions/explanations.
- Too much behavioural logic is being explained in the context file - these can be moved to relevant source code files (e.g. as docstrings at the top of files), and referenced in the context file with a statement like "refer to <file> for more info."
  - As a rule of thumb, unless it's "global orchestrating" logic that needs to be explained, keep it in a source code file's documentation.

## Architecture

### Database Layer (`rhizome/db/`)
- **models.py** — SQLAlchemy ORM models using modern `Mapped`/`mapped_column` syntax:
  - `Topic` — tree structure via adjacency list (`parent_id` self-FK). Entries attach at any depth.
  - `KnowledgeEntry`, `Tag`, `KnowledgeEntryTag` — knowledge units with tagging
  - `RelatedKnowledgeEntries` — directed graph edges between entries (acyclic, enforced via recursive CTE)
- **engine.py** — Engine factory (`get_engine`), session factory (`get_session_factory`), and `init_db()` which runs Alembic migrations then returns an engine
- **alembic/** — Alembic migration environment. Generate new migrations with `uv run alembic revision --autogenerate -m "description"`

### Database Operations (`rhizome/db/operations/`)
Pure async functions that accept `AsyncSession` as their first argument. Each module maps to a domain.

#### Key Database Patterns

- **Async-first**: All DB operations are async coroutines. Sessions come from `async_sessionmaker` with `expire_on_commit=False`.
- **Tool functions don't commit**: They call `session.flush()` but leave `commit()` to the caller, allowing transaction bundling.
- **Partial updates**: Update functions only modify fields where the argument is not `None`.
- **Cycle detection**: `add_relation()` runs a recursive CTE to check reachability before inserting a graph edge.
- **Tag normalization**: Tag names are lowercased on creation to prevent duplicates.

### TUI (`rhizome/tui`)
Textual-based TUI implementation.

REMARK: We are in the process of a major refactor - in the legacy implementation we mangled the "business logic" with the "UI logic", and we're slowly separating these pieces. The exact pattern is "model/view/view-model" - business logic is housed in the view-model, and UI/rendering logic in the view. A base ViewModelBase and ViewBase class are provided for each. 

- **IMPORTANT:** For more information on our exact MVVM pattern, refer to `docs/design-principles.md`.

## Style Guide

### Python Code

- Always follow PEP 8 style guide best practices for python code. Remember to leave adequate whitespace after "semantically cohesive" chunks of code within a larger block (function/coroutine).
- We _are_ using type annotations, but not religiously so.
- **IMPORANT:** We are adopting a 110 character max line length. This means that code, comments, and docstrings should be written to UTILIZE THE FULL EXTENT OF THIS BUDGET. It is not enough for lines to simply be "less than 110 characters", we should be using up our line length as much as possible. That said, 110 characters is not a hard limit, and letting lines fluctuate between 105-115 characters max
is totally fine.

### Documentation

Generally documentation is understood in four levels of increasing specificity:
  1. `CONTEXT.md` files.
  2. Docstrings (particularly at the top of files).
  3. Comments (in code)
  4. **The code itself**

The last level of specificity is important to keep in mind because it tempers how verbose the remaining levels of codumentation should be. Let the code speak for itself first, then explain with more layers on top of that.

Always aim for a _moderate_ amount of verbosity - explain things with specificity and brevity. DO NOT GO OVERBOARD TOO EARLY - as the code evolves, documentation will naturally grow and evolve as well, and if too much documentation is written too early, it becomes a hazard for confusion later on (contracts change, new patterns established, etc.).

### Docstrings

- Use docstrings at the top of files to explain:
  - High-level behaviour of classes/objects in the file
  - Low-level descriptions/exposition
  - Schemas/design contracts imposed (e.g. state machines, diagrams, etc.)
  - Conventions (if specific to the file, and not already present in the CONTEXT.md)
- Always aim to be _moderate_ in verbosity, neither sparse nor verbose.
- Docstrings for functions/classes generally speaking can be extremely simple - most of the code in this repo is internal, utilized by a handful of people at best, so a quick description suffices. It is rarely necessary to document args/return values/exceptions explicitly, unless requested.
- When more complete docstrings are explicitly requested, use Google-style.
- Remember: **the code is the final layer of documentation** - avoid being too redundant in docstrings, and aim to make the code its own documentation first.

### Comments

- Use comments to explain complex bits of behaviour in code, demarcate the beginning/ending of sections of code, ground a piece of code with exposition of how it fits into the rest of the codebase, etc.
- Use paragraph-long comments sparingly - aim for 1-2 sentences at most to start.
- Nicely formatted lists, tables, or diagrams in comments are encouraged (ONLY where they are necessary exposition).
- Start by being _moderate_ with comments, neither sparse nor verbose.
- Use this format for file-level header indicators (indented naturally):

```
# ========================================================================================================================
# <TITLE>
# ========================================================================================================================
```



## Misc Notes

- **IMPORTANT:** When writing documentation, write in a way that does NOT refer to past attempts. Do NOT say things like "unlike the XYZ-class" because as soon as we delete the XYZ-class, that documentation becomes inscrutable. Write documentation that describes ONLY what is _present_, and nothing more (unless explicitly requested).
- While there _is_ a `TODO.md` at the root level of the repo, you are _not_ to read it or modify it unless explicitly informed.