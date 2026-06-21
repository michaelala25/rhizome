"""AgentFactory: registration, lookup, and the option-usage hygiene warnings.

The declaration itself is plain data; the only behavior worth pinning is registration: the duplicate/
unknown-key guards and the non-fatal warnings the factory raises for builders that may be sidestepping
the snapshot/invalidation contract (declaring only live ``OptionRef``s, or taking ``OptionService``
directly). Builders here are never invoked — only their signatures are inspected — so their bodies are
empty.
"""

import warnings
from typing import Annotated

import pytest

from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.factory import AgentDeclaration, AgentFactory
from rhizome.app.options import OptionRef, Options, OptionService


def _clean(*, provider: Annotated[str, Options.Agent.Provider]): ...
def _only_live(*, ttl: Annotated[OptionRef[str], Options.Agent.Anthropic.PromptCacheTTL]): ...
def _wants_service(*, options: OptionService, provider: Annotated[str, Options.Agent.Provider]): ...


# --------------------------------------------------------------------------- #
# Registration & lookup
# --------------------------------------------------------------------------- #

def test_register_and_get():
    f = AgentFactory()
    f.register("root", build=_clean, context_schema=RootAgentContext, state_schema=dict)
    decl = f.get("root")
    assert isinstance(decl, AgentDeclaration)
    assert decl.key == "root"
    assert decl.build is _clean
    assert decl.context_schema is RootAgentContext
    assert decl.state_schema is dict and decl.response_schema is None
    assert f.declarations == (decl,)


def test_duplicate_key_raises():
    f = AgentFactory()
    f.register("root", build=_clean, context_schema=RootAgentContext)
    with pytest.raises(KeyError):
        f.register("root", build=_clean, context_schema=RootAgentContext)


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        AgentFactory().get("nope")


# --------------------------------------------------------------------------- #
# Option-usage warnings
# --------------------------------------------------------------------------- #

def test_clean_builder_warns_nothing():
    f = AgentFactory()
    with warnings.catch_warnings():
        warnings.simplefilter("error")   # any warning would fail the test
        f.register("root", build=_clean, context_schema=RootAgentContext)


def test_only_live_refs_warns():
    f = AgentFactory()
    with pytest.warns(UserWarning, match="only live OptionRef"):
        f.register("a", build=_only_live, context_schema=RootAgentContext)


def test_wants_option_service_warns():
    f = AgentFactory()
    with pytest.warns(UserWarning, match="OptionService directly"):
        f.register("b", build=_wants_service, context_schema=RootAgentContext)
