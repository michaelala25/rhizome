"""Repo-wide pytest configuration: the ``--live`` opt-in gate for real-LLM-API tests.

Tests marked ``@pytest.mark.live`` hit a paid API, so two *independent* conditions must hold before one
runs — the suite must never call out by accident:

  1. ``--live`` was passed on the command line — the explicit intent, and
  2. ``ANTHROPIC_API_KEY`` is set — the capability.

Key presence alone is deliberately not enough. The two failure modes are distinct on purpose:

  - no ``--live``           → the live tests are *deselected* (quiet; you didn't ask for them).
  - ``--live``, but no key  → they are *skipped with a reason* (loud; you asked, here's why we can't).

Run them with::

    uv run pytest tests/agent/live --live
"""

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run @pytest.mark.live tests (real LLM API; also requires ANTHROPIC_API_KEY)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: hits a real LLM API; opt-in via --live (and needs ANTHROPIC_API_KEY)"
    )


def pytest_collection_modifyitems(config, items):
    live_requested = config.getoption("--live")
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    skip_no_key = pytest.mark.skip(reason="live test: --live given but ANTHROPIC_API_KEY is not set")

    selected, deselected = [], []
    for item in items:
        if "live" not in item.keywords:
            selected.append(item)
        elif not live_requested:
            deselected.append(item)            # not asked for -> drop quietly
        else:
            if not has_key:
                item.add_marker(skip_no_key)   # asked for, but can't -> visible skip with a reason
            selected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
