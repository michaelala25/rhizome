"""Credential resolution: env var → local file fallback."""

import json
import os
import stat
from pathlib import Path
from typing import Protocol

from rhizome.config import get_config_dir

_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "voyage": "VOYAGE_API_KEY",
}


def _credentials_path() -> Path:
    return get_config_dir() / "credentials.json"


def _load() -> dict[str, str]:
    path = _credentials_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, str]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # Owner read/write only
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def get_api_key(provider: str = "anthropic") -> str | None:
    """Return API key for *provider*, checking env var first then credentials file."""
    env_var = _ENV_VARS.get(provider)
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val
    return _load().get(f"{provider}_api_key")


def store_api_key(provider: str, key: str) -> None:
    """Write an API key to the credentials file."""
    data = _load()
    data[f"{provider}_api_key"] = key
    _save(data)


def delete_api_key(provider: str) -> None:
    """Remove an API key from the credentials file (swallows missing)."""
    data = _load()
    data.pop(f"{provider}_api_key", None)
    _save(data)


def has_api_key(provider: str = "anthropic") -> bool:
    """Return whether an API key is available for *provider*."""
    return get_api_key(provider) is not None


# ==========================================================================================
# Service: APIKeyService
#   Shape : protocol + first-party impl (CredentialsAPIKeyService, below)
#   Scope : root
# ==========================================================================================


class APIKeyService(Protocol):
    """Injected, read-only resolver for provider API keys (env var → credentials file).

    Consumers depend on this rather than calling module-level ``get_api_key`` so key resolution is a
    single swappable seam — a fake store in tests, a different backend later. Mutation
    (``store_api_key`` / ``delete_api_key``) stays as module functions: writing a key is a one-shot
    admin action (the setup wizard), not something injected consumers need.
    """

    def get(self, provider: str) -> str | None: ...
    def require(self, provider: str) -> str: ...
    def has(self, provider: str) -> bool: ...


class CredentialsAPIKeyService(APIKeyService):
    """Production ``APIKeyService`` backed by this module's env→file resolution. Stateless: it reads
    on demand, so a key rotated in the environment or file is picked up without reconstruction."""

    def get(self, provider: str) -> str | None:
        return get_api_key(provider)

    def has(self, provider: str) -> bool:
        return has_api_key(provider)

    def require(self, provider: str) -> str:
        key = get_api_key(provider)
        if not key:
            env_var = _ENV_VARS.get(provider, f"{provider.upper()}_API_KEY")
            raise RuntimeError(f"No API key found for {provider!r}. Set {env_var} or run the setup wizard.")
        return key
