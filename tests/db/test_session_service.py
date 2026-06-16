"""SessionFactoryService: the DB session-factory contract, wired through the DI container.

No ``from __future__ import annotations`` -- the sample factory's parameters carry real annotation
objects, as a DI-participating module must.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rhizome.db.engine import SessionFactoryService, get_engine, get_session_factory
from rhizome.utils.services import ServiceAccessor


async def test_session_factory_descriptor_wires_engine_to_sessions():
    # get_session_factory doubles as the descriptor; its `engine` parameter is injected by type.
    engine = get_engine(":memory:")
    services = ServiceAccessor()
    services.register(AsyncEngine, engine)
    services.register_descriptor(SessionFactoryService, get_session_factory)

    sessions = services.get(SessionFactoryService)
    async with sessions() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1

    await engine.dispose()


async def test_session_factory_can_be_registered_as_a_ready_instance():
    # The simpler composition-root option: build it eagerly and register the instance.
    engine = get_engine(":memory:")
    services = ServiceAccessor()
    services.register(SessionFactoryService, get_session_factory(engine))

    sessions = services.get(SessionFactoryService)
    async with sessions() as session:
        assert (await session.execute(text("SELECT 42"))).scalar() == 42

    await engine.dispose()
