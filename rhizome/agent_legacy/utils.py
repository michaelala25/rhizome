from dataclasses import dataclass, field

from langchain.chat_models import BaseChatModel


@dataclass
class TokenUsageData:
    """Tracks token consumption and context window limits."""

    class BreakdownCategory:
        """Optional categories for breaking down token usage."""
        SYSTEM = "system"
        TOOL_MESSAGES = "tool_messages"

    total_tokens: int = 0
    max_tokens: int | None = None  # None means we couldn't determine the limit
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None

    breakdown: dict[str, int | None] = field(default_factory=lambda: {
        TokenUsageData.BreakdownCategory.SYSTEM: None,
        TokenUsageData.BreakdownCategory.TOOL_MESSAGES: None,
    })  # Optional breakdown of token usage by category

    @property
    def usage_percent(self) -> float | None:
        if self.max_tokens is None or self.max_tokens == 0:
            return None
        return (self.total_tokens / self.max_tokens) * 100


def compute_chat_model_max_tokens(chat_model: BaseChatModel):
    """Attempt to compute the maximum token count for a given chat model, returning None if
    the necessary information can't be acquired from the chat model profile.
    
    Chat model profiles are a beta feature in langchain, so this method has a number of
    passthroughs for potentially missing/incomplete info.
    """
    if not hasattr(chat_model, "profile"):
        return None
    
    profile = chat_model.profile
    if profile is None:
        return None
    
    max_tokens = profile.get("max_input_tokens", None)
    if max_tokens is None:
        return None
    
    max_tokens += profile.get("max_output_tokens", 0)
    return max_tokens