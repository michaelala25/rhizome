"""Tests for the app-context channels: ``LocalAppContextStore`` (per-branch mode + verbosity SSOT) and
``AppContextHooks`` (graph-global app-fact registry the engine renders at the tail)."""

import pytest

from rhizome.agent.app_context import (
    AppContextHooks, LocalAppContextStore, VALID_MODES, VALID_VERBOSITIES,
)


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
    assert LocalAppContextStore().mode == "idle"
    assert LocalAppContextStore(mode="learn").mode == "learn"


def test_set_mode_changes_and_emits():
    store = LocalAppContextStore()
    rec = Recorder()
    store.subscribe(store.Callbacks.OnModeChanged, rec.on_mode_changed)

    assert store.set_mode("learn") is True
    assert store.mode == "learn"
    assert rec.events == [("idle", "learn")]


def test_set_mode_idempotent_is_silent():
    store = LocalAppContextStore(mode="review")
    rec = Recorder()
    store.subscribe(store.Callbacks.OnModeChanged, rec.on_mode_changed)

    assert store.set_mode("review") is False
    assert store.mode == "review"
    assert rec.events == []


def test_set_mode_rejects_unknown_mode():
    store = LocalAppContextStore()
    with pytest.raises(ValueError):
        store.set_mode("lear")
    assert store.mode == "idle"   # left untouched on rejection


def test_copy_from_adopts_value_silently():
    parent = LocalAppContextStore(mode="learn", verbosity="verbose")
    child = LocalAppContextStore()
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
    assert LocalAppContextStore().verbosity == "auto"
    assert LocalAppContextStore(verbosity="terse").verbosity == "terse"


def test_set_verbosity_changes_and_emits():
    store = LocalAppContextStore()
    rec = Recorder()
    store.subscribe(store.Callbacks.OnVerbosityChanged, rec.on_verbosity_changed)

    assert store.set_verbosity("verbose") is True
    assert store.verbosity == "verbose"
    assert rec.verbosity_events == [("auto", "verbose")]


def test_set_verbosity_idempotent_is_silent():
    store = LocalAppContextStore(verbosity="standard")
    rec = Recorder()
    store.subscribe(store.Callbacks.OnVerbosityChanged, rec.on_verbosity_changed)

    assert store.set_verbosity("standard") is False
    assert store.verbosity == "standard"
    assert rec.verbosity_events == []


def test_set_verbosity_rejects_unknown_value():
    store = LocalAppContextStore()
    with pytest.raises(ValueError):
        store.set_verbosity("detailed")
    assert store.verbosity == "auto"   # left untouched on rejection


def test_valid_verbosities_is_the_known_set():
    assert set(VALID_VERBOSITIES) == {"terse", "standard", "verbose", "auto"}


# ------------------------------------------------------------------------------------------------
# AppContextHooks — the graph-global app-fact registry the engine renders at the tail
# ------------------------------------------------------------------------------------------------

def test_hooks_empty_renders_no_fragments():
    assert AppContextHooks().fragments() == []


def test_hooks_static_and_callable_facts_render_in_registration_order():
    hooks = AppContextHooks()
    hooks.register("model", "You are model X.")            # static-string convenience
    hooks.register("host", lambda: "Running in a TUI.")    # callable producer
    assert hooks.fragments() == ["You are model X.", "Running in a TUI."]


def test_hooks_producer_yielding_nothing_is_omitted():
    hooks = AppContextHooks()
    hooks.register("a", "A")
    hooks.register("hidden", lambda: None)                 # transient hide
    hooks.register("blank", lambda: "")                    # empty is dropped too
    hooks.register("b", "B")
    assert hooks.fragments() == ["A", "B"]


def test_hooks_register_replaces_and_unregister_removes():
    hooks = AppContextHooks()
    hooks.register("model", "old")
    hooks.register("model", "new")                         # same key replaces
    assert hooks.fragments() == ["new"] and "model" in hooks

    hooks.unregister("model")
    assert "model" not in hooks and hooks.fragments() == []
    hooks.unregister("absent")                             # no-op, no raise


def test_hooks_callable_reevaluated_each_render():
    box = {"v": "first"}
    hooks = AppContextHooks()
    hooks.register("live", lambda: box["v"])
    assert hooks.fragments() == ["first"]
    box["v"] = "second"
    assert hooks.fragments() == ["second"]                 # tracks live state, not a snapshot


# ------------------------------------------------------------------------------------------------
# AppContextHooks scoping — a child shadows its parent by key and merges for rendering
# ------------------------------------------------------------------------------------------------

def test_child_merges_parent_facts_with_parent_first():
    app = AppContextHooks()
    app.register("host", "Running in a TUI.")
    workspace = AppContextHooks(parent=app)
    workspace.register("model", "You are model X.")
    assert workspace.fragments() == ["Running in a TUI.", "You are model X."]   # parent first, then child
    assert app.fragments() == ["Running in a TUI."]        # the parent renders only its own


def test_child_shadows_parent_by_key():
    app = AppContextHooks()
    app.register("host", "generic host")
    workspace = AppContextHooks(parent=app)
    workspace.register("host", "specific host")            # same key shadows the parent's
    assert workspace.fragments() == ["specific host"]      # child value (at the parent's render position)
    assert app.fragments() == ["generic host"]             # the parent is untouched


def test_unregister_is_scope_local_and_cannot_drop_a_parent_fact():
    app = AppContextHooks()
    app.register("host", "Running in a TUI.")
    workspace = AppContextHooks(parent=app)
    workspace.unregister("host")                           # no-op: "host" isn't registered at THIS scope
    assert workspace.fragments() == ["Running in a TUI."]  # the parent fact still shows


def test_child_hides_a_parent_fact_by_shadowing_with_none():
    app = AppContextHooks()
    app.register("host", "Running in a TUI.")
    workspace = AppContextHooks(parent=app)
    workspace.register("host", lambda: None)               # explicit shadow that suppresses
    assert workspace.fragments() == []
    assert app.fragments() == ["Running in a TUI."]        # only hidden at the child scope
