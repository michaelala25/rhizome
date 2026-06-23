"""Application-wide configuration and path resolution."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import platformdirs

APP_NAME = "rhizome"


def get_config_dir() -> Path:
    """Return the OS-appropriate config directory, creating it if needed."""
    path = Path(platformdirs.user_config_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_dir() -> Path:
    """Return the OS-appropriate data directory, creating it if needed."""
    path = Path(platformdirs.user_data_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_default_db_path() -> Path:
    """Return the default database path.

    Checks ``RHIZOME_DB`` env var first, otherwise falls back to
    the platform data directory.
    """
    if env := os.environ.get("RHIZOME_DB"):
        return Path(env)
    return get_data_dir() / "rhizome.db"


def get_log_dir() -> Path:
    """Return the temp log directory, creating it if needed."""
    path = Path(tempfile.gettempdir()) / "rhizome"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_options_path() -> Path:
    """Return the path to the global options JSONC file."""
    return get_config_dir() / "options.jsonc"


# ==========================================================================================
# Service: AppConfigService
#   Shape : protocol + first-party impl (AppConfig, below)
#   Scope : root (one instance, fixed at launch)
# ==========================================================================================


class AppConfigService(Protocol):
    """Injected, read-only carrier for launch-time process configuration.

    These are the values fixed once at startup (from argv / the environment) and immutable for the
    process lifetime — distinct from ``OptionService``, which holds user-tweakable, persisted settings.
    Consumers depend on this so a launch flag reaches them as a declared dependency rather than an
    ambient global. The home for ``--debug`` and any future launch flags.
    """

    @property
    def debug(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class AppConfig(AppConfigService):
    """Production ``AppConfigService``: a frozen snapshot of the launch flags, built once from argv."""

    debug: bool = False
