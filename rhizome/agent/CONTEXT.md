# rhizome/agent/

LLM agent integration using LangChain and LangGraph.

## Architecture

Each chat tab creates its own `AgentSession`, which owns the LangChain conversation history (a list of `BaseMessage`) and a compiled agent graph. Tools are built via domain-specific builder functions (e.g. `build_core_tools`, `build_app_tools`) that close over the session factory and optional chat pane. Each tool creates its own DB session, eliminating the need for a shared session lock. `AgentContext` holds immutable per-invocation data — user settings, subagent handles, and the DB session factory — satisfying LangGraph's guideline that runtime context should be immutable. The session factory is surfaced on the context so interrupt widgets (e.g. `FlashcardReview`) can invoke DB operations without the tool having to plumb it through the interrupt payload.

### Agent Modes

The agent operates in one of three modes (idle, learn, review), controlled by `AgentMode` subclasses in `modes.py`. The active mode determines the system prompt and which tools are visible to the LLM. The chat pane is the authoritative source of truth for mode (`ChatPane.session_mode`); graph state's `mode` field follows via two paths:

- **User-initiated** (shift+tab, slash commands): `ChatPane._set_mode(source="user")` queues a pending mode change on `AgentModeMiddleware` via `set_pending_user_mode()`. On the next `abefore_model`, the middleware applies it to graph state and injects a `[System]` notification message so the agent knows.
- **Agent-initiated** (`set_mode` tool): the tool returns `Command(update={"mode": ...})` to update graph state directly via LangGraph's reducers. No notification is needed since the tool result provides context.

The system message is seeded into graph state on init with a well-known ID (`SYSTEM_PROMPT_MESSAGE_ID`). `AgentModeMiddleware.abefore_model` idempotently replaces the system message when the mode changes. Tool filtering happens statelessly in `awrap_model_call` by reading `request.state["mode"]`. No agent rebuild is required. The graph uses `RhizomeAgentState` (extends `AgentState` with a `mode: str` field) for checkpoint/replay support.

## Modules

- **config.py** — Resolves the Anthropic API key via `rhizome.credentials.get_api_key()` (env var → keyring fallback). Raises `RuntimeError` if no key is found.
- **context.py** — `AgentContext` dataclass. Holds `user_settings`, subagent handles (`answerer_subagent`, `comparator_subagent`, `scorer_subagent`, `commit_subagent`), and `session_factory` — immutable per-invocation data threaded into every tool call and into every interrupt widget's `from_interrupt(value, context)` classmethod.
- **system_prompt.py** — System prompt components split into shared and mode-specific sections. Building blocks (`SHARED_PREAMBLE`, `SHARED_APP_OVERVIEW_BASE`, `KNOWLEDGE_ENTRIES_GUIDE`, `KNOWLEDGE_ENTRIES_SUMMARY`) are composed into variants (`SHARED_APP_OVERVIEW` for learn mode, `SHARED_APP_OVERVIEW_BRIEF` for idle/review). Shared sections (`SHARED_DATABASE_CONTEXT`, `SHARED_MODE_SWITCHING`, `SHARED_SETTINGS_AND_BEHAVIOR`) are included by all modes. Mode-specific sections (`IDLE_MODE_SECTION`, `LEARN_MODE_SECTION`, `REVIEW_MODE_SECTION`) are composed by each `AgentMode` subclass. A backward-compatible `SYSTEM_PROMPT` constant is retained for subagents that don't use modes.
- **state.py** — All agent state types in one file. `RhizomeAgentState` TypedDict extending `AgentState` with `mode: str`, `review: ReviewState | None`, `flashcard_proposal_state: FlashcardProposalState | None`, and `commit_proposal_state: CommitProposalState | None`. Also defines `ReviewState`, `ReviewScope`, `ReviewConfig`, `FlashcardProposalItem`, `FlashcardProposalState`, `CommitProposalEntry`, and `CommitProposalState` TypedDicts.
- **builder.py** — Two builder functions for provider-agnostic agent construction. `build_agent(tools, provider, model_name, *, response_format, middleware, context_schema, state_schema)` is the generic builder used by subagents — it initializes the model, prepends `LogToolCallsMiddleware`, and compiles the graph. Returns `(model, agent, middleware_list)`. `build_root_agent(tools, provider, model_name, **agent_kwargs)` extends `build_agent` with root-specific features: `AgentModeMiddleware` for dynamic prompt/tool switching, optional parallel-tool and prompt-cache middleware, web tools, `AgentContext`, and `RhizomeAgentState`. Used by `AgentSession`; subagents use `build_agent` directly.
- **session.py** — `AgentSession` class encapsulating a conversation's agent graph, message history, and token usage tracking. Extracts `AgentModeMiddleware` from the middleware list returned by `build_root_agent()` and exposes `set_pending_user_mode()` for user-initiated mode changes. The system prompt is seeded as an initial `SystemMessage` with `SYSTEM_PROMPT_MESSAGE_ID`; subsequent updates are handled by `AgentModeMiddleware.abefore_model`. The `stream()` method includes the current mode in the initial graph input (`{"messages": ..., "mode": ...}`). Each session has a `thread_id` (UUID) for LangGraph checkpointing. Exposes `stream()` as a callback-driven async method (not an iterator) that accepts `on_message`, `on_update`, `on_interrupt`, and `post_chunk_handler` callbacks. User settings (answer/planning verbosity) are injected as persistent `[System]`-prefixed `HumanMessage`s in the graph state, queued only when settings change. Also contains `get_agent_kwargs(options)` for building provider-specific kwargs from Options.
- **utils.py** — `TokenUsageData` dataclass for tracking token consumption and context window limits. `compute_chat_model_max_tokens(chat_model)` derives the total context window size from a chat model's `profile` dict.
- **modes.py** — Agent operating modes (`IdleAgentMode`, `LearnAgentMode`, `ReviewAgentMode`). Each mode defines a `system_prompt` (composed from shared and mode-specific sections in `system_prompt.py`) and an `allowed_tools` frozenset (opt-in allowlist). `MODE_REGISTRY` maps mode name strings to classes. `AgentModeMiddleware` reads the active mode and applies these on every LLM call.
- **tools/** — Tool definitions and infrastructure. See `tools/CONTEXT.md`.
- **subagents/** — Subagent base classes and specialized subagents. See `subagents/CONTEXT.md`.
- **middleware/** — LangChain agent middleware components. See `middleware/CONTEXT.md`.

## Tool List

Topics: `list_topics`, `create_topics`, `delete_topics`
Entries: `list_knowledge_entries`, `read_knowledge_entries`
Flashcards: `list_flashcards`, `read_flashcards`
App Commands: `update_app_state`, `set_mode`, `ask_user_input`
Review: `review_get_past_sessions`, `review_show_session_state`, `review_update_session_state`, `review_record_interaction`, `review_present_flashcards`, `review_finish_session`
Flashcard Proposals: `flashcard_proposal_create` (with `validate=True` for inline validation), `flashcard_proposal_present`, `flashcard_proposal_edit`, `flashcard_proposal_accept`
Commit: `commit_show_selected_messages`, `commit_invoke_subagent`, `commit_proposal_create`, `commit_proposal_present`, `commit_proposal_edit`, `commit_proposal_accept`
SQL: `execute_sql`
