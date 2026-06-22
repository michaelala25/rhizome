# rhizome/agent_legacy/middleware/

LangChain agent middleware components for modifying model requests and responses.

## Modules

- **agent_mode.py** — `AgentModeMiddleware`, synchronizes mode between the chat pane (source of truth) and graph state. Uses two hooks: `abefore_model` checks for pending user-initiated mode changes (set via `set_pending_user_mode()`) and updates `state["mode"]` + the system prompt accordingly, injecting a `[System]` notification for user-initiated changes; `awrap_model_call` statelessly filters `request.tools` by reading `request.state["mode"]`. Agent-initiated mode changes (the `set_mode` tool) update graph state directly via `Command(update={"mode": ...})` — the middleware just keeps the system prompt in sync. An `asyncio.Lock` protects the pending user mode slot from races. The initial system message is seeded in `AgentSession.__init__` with well-known ID `SYSTEM_PROMPT_MESSAGE_ID`.
- **penultimate_cache.py** — `AnthropicPenultimateCacheMiddleware`, places a `cache_control` breakpoint on the penultimate message so that Anthropic's API treats everything before it as a stable, cacheable prefix. Configurable via `ttl` or a custom `cache_control` dict.
- **log_tool_calls.py** — `LogToolCallsMiddleware`, logs every tool invocation at DEBUG level with full arguments via the `wrap_tool_call`/`awrap_tool_call` hooks. Always enabled.
- **disable_parallel_tools.py** — `DisableParallelToolCallsMiddleware`, injects `parallel_tool_calls=False` into `model_settings` on every request. Each tool now has its own session so this is no longer strictly needed for DB safety, but remains as a user-configurable option.
