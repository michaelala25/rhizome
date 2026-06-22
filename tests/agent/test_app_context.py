"""Tests for AppContextStore — the live SSOT for per-branch app settings (mode + verbosity)."""

import pytest

from rhizome.agent.app_context import AppContextStore, VALID_MODES, VALID_VERBOSITIES


class Recorder:
    """Strong-referenced subscriber — CallbackHost holds callbacks weakly, so a bare lambda would die."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.verbosity_events: list[tuple[str, str]] = []

    def on_mode_changed(self, old: str, new: str) -> None:
        self.events.append((old, new))

    def on_verbosity_changed(self, old: str, new: str) -> None:
        self.verbosity_events.append((old, new))


def test_default_and_custom_initial_mode():
    assert AppContextStore().mode == "idle"
    assert AppContextStore(mode="learn").mode == "learn"


def test_set_mode_changes_and_emits():
    store = AppContextStore()
    rec = Recorder()
    store.subscribe(store.Callbacks.OnModeChanged, rec.on_mode_changed)

    assert store.set_mode("learn") is True
    assert store.mode == "learn"
    assert rec.events == [("idle", "learn")]


def test_set_mode_idempotent_is_silent():
    store = AppContextStore(mode="review")
    rec = Recorder()
    store.subscribe(store.Callbacks.OnModeChanged, rec.on_mode_changed)

    assert store.set_mode("review") is False
    assert store.mode == "review"
    assert rec.events == []


def test_set_mode_rejects_unknown_mode():
    store = AppContextStore()
    with pytest.raises(ValueError):
        store.set_mode("lear")
    assert store.mode == "idle"   # left untouched on rejection


def test_copy_from_adopts_value_silently():
    parent = AppContextStore(mode="learn", verbosity="verbose")
    child = AppContextStore()
    rec = Recorder()
    child.subscribe(child.Callbacks.OnModeChanged, rec.on_mode_changed)
    child.subscribe(child.Callbacks.OnVerbosityChanged, rec.on_verbosity_changed)

    child.copy_from(parent)
    assert child.mode == "learn"
    assert child.verbosity == "verbose"
    assert rec.events == []             # branch seeding is not a user/agent change
    assert rec.verbosity_events == []


def test_valid_modes_is_the_known_set():
    assert set(VALID_MODES) == {"idle", "learn", "review"}


def test_default_and_custom_initial_verbosity():
    assert AppContextStore().verbosity == "auto"
    assert AppContextStore(verbosity="terse").verbosity == "terse"


def test_set_verbosity_changes_and_emits():
    store = AppContextStore()
    rec = Recorder()
    store.subscribe(store.Callbacks.OnVerbosityChanged, rec.on_verbosity_changed)

    assert store.set_verbosity("verbose") is True
    assert store.verbosity == "verbose"
    assert rec.verbosity_events == [("auto", "verbose")]


def test_set_verbosity_idempotent_is_silent():
    store = AppContextStore(verbosity="standard")
    rec = Recorder()
    store.subscribe(store.Callbacks.OnVerbosityChanged, rec.on_verbosity_changed)

    assert store.set_verbosity("standard") is False
    assert store.verbosity == "standard"
    assert rec.verbosity_events == []


def test_set_verbosity_rejects_unknown_value():
    store = AppContextStore()
    with pytest.raises(ValueError):
        store.set_verbosity("detailed")
    assert store.verbosity == "auto"   # left untouched on rejection


def test_valid_verbosities_is_the_known_set():
    assert set(VALID_VERBOSITIES) == {"terse", "standard", "verbose", "auto"}
