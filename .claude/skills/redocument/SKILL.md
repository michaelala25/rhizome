---
name: redocument
description: Rewrite CONTEXT.md files and in-file docstrings/comments across a directory tree to cut bloat, drop stale references, and tighten focus to what each directory actually contains. Use whenever the user asks to "redocument," "clean up the docs in," "rewrite the CONTEXT.md files for," or "freshen up the comments under" a directory — or describes documentation that has drifted, grown verbose, or accumulated references to code that no longer exists. Trigger even if the user doesn't say "skill" or name CONTEXT.md explicitly; phrases like "the docs in src/ have gotten out of hand" or "can you simplify the docstrings under this folder" are the same request.
---

# Redocument a Directory

A skill for rewriting CONTEXT.md files and in-file documentation across a directory tree. The goal is **less documentation, better placed** — not a comprehensive rewrite that mirrors what the code already says.

## What this skill is for

The user has a directory whose documentation has drifted: CONTEXT.md files bloated with exposition, docstrings explaining things in five paragraphs that need one, comments referencing renamed-or-deleted code ("unlike the old `FooWidget`..."). They want it cleaned up across the whole tree, not file by file.

This skill assumes Claude Code with the Task tool available, so directories can be redocumented in parallel by subagents.

## Core principles

Internalize these before touching anything. They are the whole point of the skill — every later step is just mechanics.

**Code is the final layer of documentation.** Docstrings and CONTEXT.md files exist to summarize and orient, not to recapitulate. If a docstring is restating what five lines of obvious code do, delete the docstring. If a CONTEXT.md is paraphrasing every function signature in the directory, cut it.

**Document the present, not the past.** Phrases like "unlike the old `XYZWidget`," "we no longer use the legacy approach because...," or "this replaces the deprecated handler" are documentation debt. The thing being contrasted is gone; the contrast is dead weight. Keep general guidance ("prefer composition over inheritance here because of N") when it informs future decisions; cut named references to things that no longer exist.

**Stay scoped to the current directory.** Each CONTEXT.md describes what lives in *its* directory. Seams with neighbouring directories can be mentioned briefly, but don't re-explain what a parent or child directory already covers. Redundant documentation across levels is a smell.

**Depth follows necessity.** Start with what the thing does at the highest level and how it fits in. Go deeper only for edge cases, coupling, or non-obvious behaviour. If complex behaviour merits a thorough explanation, that explanation lives in the **top-of-file docstring of the relevant code file**, not in CONTEXT.md. CONTEXT.md can then say "see `widget.py` for the full state-machine details."

**Compact, not terse.** Cut irrelevant exposition; do not cut clarity. A good comment uses most of a 110-character line (treat 105–115 as the comfortable zone, not a hard rule). Dense jargon to save tokens is the wrong kind of brevity.

## Workflow

### 1. Survey the tree

Before spawning anything, walk the directory recursively and build a mental map:

- Use `find <root> -type d` and `find <root> -name CONTEXT.md` to list directories and existing context files.
- Read every existing CONTEXT.md. They are the user's prior attempt at the same job — flawed but informative.
- Skim the code files in each directory enough to know what lives there (names and one-line purposes are usually enough — you don't need to deeply understand every file at this stage; the subagents will do that).
- Note which directories look healthy and which look bloated. The bloated ones often have the most stale references.

Output of this step: a list of directories to redocument, plus any early observations to feed to the subagents (e.g., "the `legacy/` references in `core/` look like prime stale-reference candidates").

### 2. Spawn one subagent per directory, in parallel

For each directory in the tree, spawn a Task subagent with the brief below. Spawn them in **the same turn** so they run concurrently — this is the whole reason the skill exists at this scale.

Each subagent's brief should contain:

- **The target directory** (absolute path).
- **The full "Core principles" section above**, verbatim. These are the rules the subagent is being held to.
- **The per-directory instructions** (next subsection).
- **Scope boundary:** "Only modify files inside `<this directory>`. Do not touch parents, children, or siblings."
- **Any early observations** from step 1 relevant to this directory.

#### Per-directory instructions (give these to each subagent)

> Rewrite the CONTEXT.md and the docstrings/top-of-file comments for every code file in this directory. Follow the core principles above.
>
> **CONTEXT.md should answer, briefly:**
> 1. What does this directory contain, at the highest level?
> 2. How does it fit into the bigger picture (one or two sentences about its role relative to neighbours)?
> 3. What are the entry points or key files someone should look at first?
> 4. Any non-obvious coupling, gotchas, or shared state — *only if it would surprise a reader*. For deep explanations, point to the relevant code file: "See `parser.py` for the full grammar."
>
> **Docstrings and top-of-file comments should:**
> - Lead with what the thing does and why it exists.
> - Mention how it's used or what calls it, if non-obvious.
> - Describe edge cases, invariants, or coupling only when the behaviour warrants it.
> - Stay close to a 110-character line budget. 105–115 is the comfortable zone.
> - Cut every reference to renamed, deleted, or "old" code. If general guidance can be salvaged from a dead reference, keep the guidance and drop the name.
>
> **Things to actively delete:**
> - Paragraphs restating what the next ten lines of code obviously do.
> - "Unlike the old X" / "we used to do Y" / "this replaces Z" references where X, Y, Z no longer exist or aren't reachable from the current code.
> - Redundant documentation that the parent or child CONTEXT.md already covers.
> - Filler that exists because someone felt the section needed filling.
>
> Make a judgment call on depth. Brief is good; cryptic is not. When in doubt about whether a paragraph earns its place, cut it — the reader can always read the code.

### 3. Cross-layer review (you, not subagents)

Once the subagents finish, the per-directory work is done but the *tree* hasn't been reviewed as a whole. Do this yourself:

- **Look for cross-cutting concerns** that no single directory owns but that span several — e.g., a serialization protocol used in three places, an event bus, a shared lifecycle. If the same concept needed explaining in multiple CONTEXT.md files, that's a signal it should live in **one** shared overview file instead, with the others pointing to it.
- **Look for missing seams.** If directory A and directory B communicate in a non-obvious way, and neither CONTEXT.md mentions the other, the seam is undocumented.
- **Look for redundancy across levels.** If a parent CONTEXT.md and a child CONTEXT.md say the same thing, decide which one should keep it (usually the parent, with the child trimmed).

Present these findings to the user as **suggestions**, not unilateral edits. The user picks where the cross-layer documentation lives. Example framing: "I noticed three directories all describe the event-dispatch protocol. I'd suggest consolidating into a `docs/events.md` or a top-level CONTEXT.md section — which do you prefer?"

### 4. Hand back

Summarize what was changed (file counts, not a wall of diffs), then list the cross-layer suggestions from step 3 for the user to confirm. Don't claim the job is "done" until the cross-layer pass has been resolved — that's the step that turns a per-directory cleanup into a coherent re-documentation.

## Anti-patterns to watch for in your own output

A few failure modes that show up when running this skill:

- **Treating "rewrite" as "expand."** If your new CONTEXT.md is longer than the old one, you probably missed the point. Length is not the goal, but if you're not net-reducing somewhere, re-examine.
- **Subagents going out of bounds.** Each subagent should only edit files in its assigned directory. If you find a subagent has modified a parent or sibling, that's a scope violation — undo it and re-scope.
- **Cargo-culting old structure.** If the existing CONTEXT.md has six headers, the new one doesn't have to. Pick the structure that fits what's actually in the directory.
- **Documenting the trivial.** A directory with three short utility files probably needs three sentences of CONTEXT.md, not three paragraphs. Match the documentation to the substance.
- **Hedging the cross-layer pass.** Step 3 is the part that's easiest to skip and most valuable to keep. If you finish the parallel subagent runs and immediately tell the user "all done," you've skipped the integration step that makes the whole thing coherent.
