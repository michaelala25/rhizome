# Agent Workflow Architecture

## Overview

The agent supports rich, multi-step workflows where tool calls are visible to the user, the agent can pause for user input via interrupts, and multi-step plans execute with progress updates. The system is built on LangGraph with a callback-driven streaming architecture that integrates tightly with the Textual TUI.

## Checkpointing

`InMemorySaver` provides LangGraph checkpointing, which is required for interrupt/resume support. Each `ChatPane` has a `thread_id` (UUID) stored on `AgentSession` and passed through config to LangGraph. This enables mid-conversation pauses and resumptions across multiple exchanges.

## Streaming Architecture

The implementation uses a **callback-driven** pattern rather than an iterator-based event type approach. `AgentSession.stream()` accepts four callbacks:

- **`on_message`** — called when a new message is available
- **`on_update`** — called for incremental state updates
- **`on_interrupt`** — called when the agent pauses for user input
- **`post_chunk_handler`** — called after each streamed chunk for UI synchronization

The TUI's `AgentMessageHarness` widget orchestrates the display of streamed content, while `ChatPane._run_agent` manages the overall flow. This callback approach integrates naturally with Textual's reactive UI model.

## Tool Call Visibility

Tool calls are displayed via a `ToolCallList` widget that shows which tools are being called and their results. These appear in the chat as collapsible elements, giving users transparency into what the agent is doing without cluttering the conversation.

## Interrupt System

Interrupts use LangGraph's `interrupt()` primitive. When an interrupt fires, the agent pauses, the TUI displays the appropriate widget, and on user response the agent resumes via `Command(resume=...)`.

Four interrupt types are implemented, each with a dedicated TUI widget:

- **Choices** (`choices.py`) — the agent presents a set of options for the user to pick from
- **Multiple choice** (`multiple_choices.py`) — multi-select variant where the user can choose several options
- **Warning** (`warning.py`) — the agent warns the user before a destructive action, requesting confirmation
- **Commit proposal** — a specialized interrupt for reviewing knowledge entry proposals before committing them to the database

## Agent Modes

Three modes control agent behavior: **idle**, **learn**, and **review**. These are implemented as `AgentMode` subclasses in `modes.py`. The active mode determines:

- **System prompt** — composed from shared sections plus mode-specific sections
- **Tool visibility** — each mode defines an opt-in allowlist of tools the LLM can see

Mode changes can be user-initiated (Shift+Tab, slash commands) or agent-initiated (via the `set_mode` tool). `AgentModeMiddleware` handles prompt swapping and tool filtering statelessly on every LLM call, so mode changes are instant with no graph recompilation.

## Thread and Session Management

`AgentSession` encapsulates the conversation's agent graph, message history, and token usage tracking. Each session has a `thread_id` for LangGraph checkpointing. The system prompt is seeded with a well-known ID (`SYSTEM_PROMPT_MESSAGE_ID`) and updated idempotently by middleware on each invocation.

User settings are injected as persistent `[System]`-prefixed `HumanMessage`s, queued only when settings change.

## Subagent Architecture

`Subagent` is a lightweight dataclass providing agents with isolated conversation history. Subagents support two modes:

- **Stateful** — multi-turn conversations with persistent history
- **Stateless** — single-shot invocations with no retained context

`StructuredSubagent` extends the base with JSON response parsing for structured output. The commit workflow uses a specialized `CommitSubagent` for extracting knowledge entries from conversation messages.

## Key Design Decisions

- **Callback-driven streaming** over iterator-based event types — integrates better with Textual's reactive UI
- **Tool filtering via middleware** rather than graph rebuild — mode changes are instant, no recompilation needed
- **Per-tool DB sessions** — each tool creates its own database session, eliminating shared session locking
- **Immutable invocation context** — `AgentContext` holds only immutable per-invocation data (`user_settings`)

## Key Files

| Area | File |
|------|------|
| Session and streaming | `rhizome/agent_legacy/session.py` |
| Agent graph construction | `rhizome/agent_legacy/builder.py` |
| Mode system | `rhizome/agent_legacy/modes.py` |
| Tool definitions | `rhizome/agent_legacy/tools.py` |
| Review-mode tools | `rhizome/agent_legacy/review_tools.py` |
| Commit workflow | `rhizome/agent_legacy/commit.py` |
| Subagent framework | `rhizome/agent_legacy/subagent.py` |
| Middleware | `rhizome/agent_legacy/middleware/` |
| Chat orchestration | `rhizome/tui/widgets/chat_pane.py` |
| Stream display | `rhizome/tui/widgets/agent_message_harness.py` |
| Interrupt widgets | `rhizome/tui/widgets/interrupt.py`, `choices.py`, `multiple_choices.py`, `warning.py` |
