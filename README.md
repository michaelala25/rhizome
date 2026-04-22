# Rhizome

A terminal-based knowledge management system powered by Claude. Learn, capture, and retain knowledge from your terminal.

<!-- TODO: demo gif -->
<!-- ![Demo](assets/demo.gif) -->
<p align="center">
  <img src="docs/demo.gif" alt="Career-Ops Demo" width="800">
</p>

## What This Is

- Rhizome is first and foremost an **AI study partner**. Chat about any topic and commit what you've learned into a structured, reviewable format directly from the terminal.
- Rhizome helps you **automatically develop an understanding of topics** - learn about new things and automatically build connections with prior understanding drawn from your knowledge database.
- **Generate review sessions** from your knowledge database to practice retention and understanding of your topics, tailored to your curriculum, and track your performance over time.
- **Upload documents** and use Rhizome as a **tag-along study partner/test maker**.
- **A terminal-native TUI built on [Textual](https://github.com/Textualize/textual).**

## Why I Made It

There were two problems I had in mind that I wanted to tackle:

1. I spend a lot of time asking LLMs for answers to random technical questions, but very little time on building my _own_ understanding. So I built a terminal application that helps me do both. Instead of asking Claude for a random command and immediately forgetting it, I can automatically create a knowledge entry, tie it to my other knowledge entries to build a richer understanding, generate reviews to practice recalling said knowledge, and track my retention/understanding over time. The goal is a seamless experience of "asking Claude for help" and simultaneously developing my own understanding.

2. In the past I've used apps like Anki to help memorize topics, but I struggled to build up an _understanding_ through flashcards alone. I wanted to build an AI tutor that could automatically consolidate my learning into concise, atomic "knowledge entries", and then later help me review in a richer/more dynamic format than flashcards alone. Review mode is basically "office-hours" with your AI professor, tailored to your knowledge base.

## Usage

Rhizome has three operating modes, each designed for a different part of the learning cycle:

### Learn Mode

Chat with Claude about a topic. When you've covered something worth keeping, use `/commit` to extract structured knowledge entries from the conversation. The agent proposes entries, you review and edit them, and approved entries get saved to your knowledge base.

<!-- TODO: learn mode gif -->
<!-- ![Learn mode](assets/learn.gif) -->

### Review Mode

Practice what you've learned. Choose a topic, pick a review style (flashcard, conversation, or mixed), and the agent builds a session from your existing knowledge and flashcards. Answers are scored on a 1-4 scale - either by you or automatically by a scoring subagent.

<!-- TODO: review mode gif -->
<!-- ![Review mode](assets/review.gif) -->

### Resource Management

Upload PDFs and other documents, link them to topics, and let Rhizome utilize them in context for discussion, knowledge extraction, verification against an authority, etc.

<!-- TODO: resource management gif -->
<!-- ![Resources](assets/resources.gif) -->

## Features

- **Triphasic agent** - idle, learn, and review modes with mode-specific tools and system prompts, switchable mid-conversation
- **Knowledge commit workflow** - extract structured entries from freeform chat via an interactive proposal/review flow
- **Configurable review sessions** - flashcard, conversational, or mixed review; manual or auto-scored; immediate or batched critique; session history ranked by topic overlap for continuity
- **Document processing pipeline** - PDF extraction with automatic section detection, LLM-refined structure, and per-section chunk storage
- **Tabbed sessions** - multiple independent chat sessions with per-tab option overrides
- **Hierarchical topic tree** - organize knowledge in nested topics with tag and relation support between entries

## Quick Start

> Rhizome is in active development. These instructions may change.

```bash
# Clone and install
git clone https://github.com/michaelala25/rhizome.git
cd rhizome
uv sync

# Launch
uv run python -m rhizome.tui
```

On first launch, you'll be prompted to enter your name and Anthropic API key.

**Requires:** Python 3.14+, [uv](https://docs.astral.sh/uv/)
