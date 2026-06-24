"""Tests for StatusBarModel — the status bar's projection over the per-branch LocalAppContextStore
(mode/verbosity) and the conversation-global OptionService (model name)."""

from rhizome.agent.app_context import LocalAppContextStore
from rhizome.app.chat_area.status import StatusBarModel
from rhizome.app.options import Options, OptionScope


class DirtyRecorder:
    """Strong-referenced OnDirty subscriber (CallbackHost holds callbacks weakly)."""

    def __init__(self, vm: StatusBarModel) -> None:
        self.count = 0
        vm.subscribe(vm.Callbacks.OnDirty, self._on_dirty)

    def _on_dirty(self) -> None:
        self.count += 1


def _options() -> Options:
    return Options(OptionScope.Session)


def test_projects_initial_mode_verbosity_and_model():
    options = _options()
    bar = StatusBarModel(options, LocalAppContextStore(mode="learn", verbosity="verbose"))

    assert bar.mode == "learn"
    assert bar.verbosity == "verbose"
    assert bar.model_name == options.get(Options.Agent.Model)


def test_no_options_leaves_model_blank():
    bar = StatusBarModel(None, LocalAppContextStore())
    assert bar.model_name == ""
    assert bar.mode == "idle"
    assert bar.verbosity == "auto"


def test_reacts_to_app_state_mode_and_verbosity_changes():
    app_state = LocalAppContextStore()
    bar = StatusBarModel(None, app_state)
    rec = DirtyRecorder(bar)

    app_state.set_mode("review")
    app_state.set_verbosity("terse")

    assert bar.mode == "review"
    assert bar.verbosity == "terse"
    assert rec.count == 2


def test_set_app_state_swaps_subscription_and_rereads():
    first = LocalAppContextStore(mode="learn")
    second = LocalAppContextStore(mode="review", verbosity="terse")
    bar = StatusBarModel(None, first)
    rec = DirtyRecorder(bar)

    bar.set_app_state(second)            # re-reads from the new store, one dirty
    assert (bar.mode, bar.verbosity) == ("review", "terse")
    assert rec.count == 1

    first.set_mode("review")             # the old store is detached — no effect
    assert rec.count == 1

    second.set_mode("learn")             # the new store drives the bar
    assert bar.mode == "learn"
    assert rec.count == 2


def test_reacts_to_model_option_change():
    options = _options()
    bar = StatusBarModel(options, LocalAppContextStore())
    rec = DirtyRecorder(bar)

    new_model = "claude-sonnet-4-6"
    assert bar.model_name != new_model
    options.set(Options.Agent.Model, new_model)

    assert bar.model_name == new_model
    assert rec.count == 1


def test_provider_change_rereads_conditional_model():
    options = _options()
    bar = StatusBarModel(options, LocalAppContextStore())

    options.set(Options.Agent.Provider, "openai")
    # Model's effective value is conditional on Provider; the bar re-reads it off the option service
    # rather than trusting a single Model change to cover the conditional default.
    assert bar.model_name == options.get(Options.Agent.Model)
