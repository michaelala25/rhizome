"""App-facing interaction tools.

- ``ask_user_input`` pauses the run on a LangGraph ``interrupt`` to put one or more multiple-choice
  questions in front of the user, then resumes with their selection. The interrupt value's ``type``
  (``"choices"`` for a single question, ``"multiple_choice"`` for several) is the key the chat layer's
  stream router uses to build the matching feed widget (see ``rhizome/app/chat_area/stream_router``);
  whatever that widget resolves with is what ``interrupt()`` returns here.

- ``set_mode`` switches the active conversation mode. It writes the live ``LocalAppContextStore`` on the
  context (``ctx.app_state``) rather than returning a state update: that store is the single source of
  truth both the user and the agent write through, and the prompt engine commits the change into
  ``RootAgentState["mode"]`` at the next compile (see ``rhizome/agent/app_context.py``).

- ``cleanup_context`` / ``hydrate`` are the two sides of context reclamation: ``cleanup_context`` files a
  ``CleanupRequest`` to clear a group now, ``hydrate`` files a ``HydrateRequest`` to keep one longer. Both
  only express intent onto the relevant ``pending_*`` channel; the prompt engine is the sole emitter of the
  edits and applies them at the next compile, honoring them only while auto-compaction is on (see
  ``engine.cleanup``).
"""

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from ..app_context import VALID_MODES
from ..base import CleanupRequest
from .visibility import ToolVisibility, tool_visibility


class Question(BaseModel):
    """A single multiple-choice question presented to the user."""

    name: str = Field(description="Short tab label (1-2 words)")
    prompt: str = Field(description="Full question text shown to the user")
    options: list[str] = Field(description="List of option strings to choose from")


_ASK_USER_INPUT_DESC = (
    "Present one or more multiple-choice questions to the user and wait for their selections. Use this "
    "when you need the user to choose between options before proceeding.\n\n"
    "Each question has a short tab name (1-2 words), a full prompt, and a list of options. A single "
    "question shows a simple choice widget; multiple questions are presented as a tabbed widget where the "
    "user answers each in turn."
)

_SET_MODE_DESC = (
    "Set the active session mode. Accepted values: 'idle', 'learn', 'review'. The switch takes effect "
    "from your next step; you do not need to repeat it."
)

_CLEANUP_CONTEXT_DESC = (
    "Free up context space by clearing earlier reclaimable content you no longer need. Bulky tool results "
    "are tagged inline with a '[reclaimable · <group>]' marker, where <group> is the source tool's name; "
    "pass that group here to clear every result in it. The content is replaced with a short placeholder — "
    "the tool call and its result stay in place, just emptied — and read-only results can always be "
    "re-fetched by calling the tool again. Pass strategy='summarize' to instead replace the content with a "
    "concise summary (slower, but keeps the gist) rather than emptying it. Takes effect from your next step."
)

_HYDRATE_DESC = (
    "Keep reclaimable content in context longer. Reclaimable tool results (tagged '[reclaimable · "
    "<group>]') are otherwise cleared automatically once they age out; pass a group here to push that "
    "back when you still need it. Repeatedly keeping the same group eventually makes it permanent. Use "
    "this instead of cleanup_context when content is still useful. Takes effect from your next step."
)


def build_app_tools() -> dict:
    """Build the app-facing interaction tools (name -> tool), following the ``build_*_tools`` convention."""

    @tool_visibility(ToolVisibility.LOW)
    @tool("ask_user_input", description=_ASK_USER_INPUT_DESC)
    async def ask_user_input_tool(questions: list[Question]) -> str:
        if len(questions) == 1:
            q = questions[0]
            result = interrupt({"type": "choices", "message": q.prompt, "options": q.options})
            return f"User selected: {result}"

        result = interrupt({"type": "multiple_choice", "questions": [q.model_dump() for q in questions]})
        # ``result`` maps each question's name to the chosen answer.
        lines = [f"{name}: {answer}" for name, answer in result.items()]
        return "User selections:\n" + "\n".join(lines)

    @tool_visibility(ToolVisibility.LOW)
    @tool("set_mode", description=_SET_MODE_DESC)
    async def set_mode_tool(mode: str, runtime: ToolRuntime) -> str:
        # The agent expresses the switch by writing the live store; the prompt engine commits it into
        # RootAgentState at the next compile and reacts (mode guide/header). No Command — see module docstring.
        store = getattr(runtime.context, "app_state", None)
        if store is None:
            return "Mode control is unavailable in this conversation."
        if mode not in VALID_MODES:
            return f"Invalid mode {mode!r}. Must be one of: {', '.join(VALID_MODES)}."
        store.set_mode(mode)
        return f"Mode is now: {mode}."

    @tool_visibility(ToolVisibility.LOW)
    @tool("cleanup_context", description=_CLEANUP_CONTEXT_DESC)
    async def cleanup_context_tool(group: str, runtime: ToolRuntime, strategy: str = "stub") -> Command:
        # File the declarative request, with this call's own tool_result in the same update; the engine
        # reclaims the group at the next compile (it is the sole emitter — see PromptEngine._cleanup).
        # ``strategy`` rides along: "summarize" condenses the content, anything else empties it to a stub.
        request: CleanupRequest = {"group": group}
        if strategy == "summarize":
            request["strategy"] = "summarize"
        verb = "summarized" if strategy == "summarize" else "cleared"
        return Command(update={
            "pending_cleanups": [request],
            "messages": [ToolMessage(
                content=f"Scheduled reclaimable '{group}' content to be {verb} on your next step.",
                tool_call_id=runtime.tool_call_id,
            )],
        })

    @tool_visibility(ToolVisibility.LOW)
    @tool("hydrate", description=_HYDRATE_DESC)
    async def hydrate_tool(group: str, runtime: ToolRuntime) -> Command:
        # File the keep-it-longer request; the engine pushes the group's expiry out (or settles it) next
        # compile (sole emitter — see PromptEngine._cleanup / engine.cleanup.apply_hydrations).
        return Command(update={
            "pending_hydrations": [{"group": group}],
            "messages": [ToolMessage(
                content=f"Keeping reclaimable '{group}' content in context longer.",
                tool_call_id=runtime.tool_call_id,
            )],
        })

    return {
        "ask_user_input": ask_user_input_tool,
        "set_mode": set_mode_tool,
        "cleanup_context": cleanup_context_tool,
        "hydrate": hydrate_tool,
    }
