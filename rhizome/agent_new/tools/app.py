"""App-facing interaction tools.

- ``ask_user_input`` pauses the run on a LangGraph ``interrupt`` to put one or more multiple-choice
  questions in front of the user, then resumes with their selection. The interrupt value's ``type``
  (``"choices"`` for a single question, ``"multiple_choice"`` for several) is the key the chat layer's
  stream router uses to build the matching feed widget (see ``rhizome/app/chat_area/stream_router``);
  whatever that widget resolves with is what ``interrupt()`` returns here.

- ``set_mode`` switches the active conversation mode. It writes the live ``AppContextStore`` on the
  context (``ctx.app_state``) rather than returning a state update: that store is the single source of
  truth both the user and the agent write through, and the prompt engine commits the change into
  ``AgentState["mode"]`` at the next compile (see ``rhizome/agent_new/app_context.py``).
"""

from langchain.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ..app_context import VALID_MODES
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
        # AgentState at the next compile and reacts (mode guide/header). No Command — see module docstring.
        store = getattr(runtime.context, "app_state", None)
        if store is None:
            return "Mode control is unavailable in this conversation."
        if mode not in VALID_MODES:
            return f"Invalid mode {mode!r}. Must be one of: {', '.join(VALID_MODES)}."
        store.set_mode(mode)
        return f"Mode is now: {mode}."

    return {"ask_user_input": ask_user_input_tool, "set_mode": set_mode_tool}
