"""Environment-based configuration for the LLM agent."""

from rhizome.credentials import get_api_key as _resolve_key


def get_api_key() -> str:
    """Read the Anthropic API key from env var or system keyring."""
    key = _resolve_key("anthropic")
    if not key:
        raise RuntimeError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY or run the setup wizard."
        )
    return key
