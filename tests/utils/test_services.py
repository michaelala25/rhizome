"""ServiceAccessor: annotation-driven DI -- injection edges, cycle detection, scoping.

This module intentionally omits ``from __future__ import annotations`` so that the sample factories
below carry real annotation objects, exactly as a participating production module must.
"""

import pytest

from rhizome.utils.services import (
    CyclicalServiceDependencyError,
    DuplicateRegistrationError,
    Handle,
    ServiceAccessor,
    ServiceError,
    ServiceMeta,
    ServiceNotFoundError,
)


# --------------------------------------------------------------------------- #
# Sample services (module-level so their annotations resolve to real types)
# --------------------------------------------------------------------------- #

class Database:
    def __init__(self):
        self.connected = True


class Repo:
    def __init__(self, *, db: Database):
        self.db = db


class Consumer:
    def __init__(self, *, db: Handle[Database]):   # weak edge
        self.db = db


class Locator:
    def __init__(self, *, accessor: ServiceAccessor):   # locator edge
        self.accessor = accessor


class Configurable:
    def __init__(self, *, db: Database, limit: int = 10):   # limit is configuration, not a dependency
        self.db = db
        self.limit = limit


class Provenanced:
    def __init__(self, *, db: Database, meta: ServiceMeta):   # self-meta edge: its own provenance
        self.db = db
        self.meta = meta


# Marker key types, defined before the factories that reference them, so mutual / cyclic shapes have
# real annotations available (mirrors the "key = protocol/marker, separate from impl" convention).
class AKey: pass
class BKey: pass
class XKey: pass
class YKey: pass
class Marker: pass


class ServiceA:
    def __init__(self, *, b: Handle[BKey]):
        self.b = b


class ServiceB:
    def __init__(self, *, a: Handle[AKey]):
        self.a = a


class StrongX:
    def __init__(self, *, y: YKey):
        self.y = y


class StrongY:
    def __init__(self, *, x: XKey):
        self.x = x


# --------------------------------------------------------------------------- #
# Strong edges, caching, instances
# --------------------------------------------------------------------------- #

def test_strong_injection_resolves_and_caches():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    s.register_descriptor(Repo)

    repo = s.get(Repo)
    assert isinstance(repo, Repo)
    assert isinstance(repo.db, Database)
    # Cached: descriptors fire once per scope.
    assert s.get(Repo) is repo
    assert s.get(Database) is repo.db


def test_register_instance_used_as_strong_dependency():
    s = ServiceAccessor()
    db = Database()
    s.register(Database, db)
    s.register_descriptor(Repo)
    assert s.get(Repo).db is db


def test_missing_strong_dependency_raises_on_resolution():
    s = ServiceAccessor()
    s.register_descriptor(Repo)   # Database never registered
    with pytest.raises(ServiceNotFoundError):
        s.get(Repo)


# --------------------------------------------------------------------------- #
# Weak edges (Handle[...])
# --------------------------------------------------------------------------- #

def test_weak_handle_is_lazy_and_resolves_on_get():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    s.register_descriptor(Consumer)

    c = s.get(Consumer)
    assert isinstance(c.db, Handle)        # injected unresolved
    assert Database not in s._instances    # target not built yet

    resolved = c.db.get()
    assert isinstance(resolved, Database)
    assert s.get(Database) is resolved


def test_weak_mutual_reference_has_no_construction_cycle():
    s = ServiceAccessor()
    s.register_descriptor(AKey, ServiceA)
    s.register_descriptor(BKey, ServiceB)
    s.validate()   # weak edges are excluded from the cycle check

    a = s.get(AKey)
    b = s.get(BKey)
    assert a.b.get() is b
    assert b.a.get() is a


def test_public_handle_defers_resolution():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    h = s.handle(Database)
    assert isinstance(h, Handle)
    assert isinstance(h.get(), Database)


# --------------------------------------------------------------------------- #
# Locator edge
# --------------------------------------------------------------------------- #

def test_accessor_locator_injection():
    s = ServiceAccessor()
    s.register_descriptor(Locator)
    assert s.get(Locator).accessor is s


# --------------------------------------------------------------------------- #
# Cycle detection
# --------------------------------------------------------------------------- #

def test_strong_cycle_detected_at_resolution():
    s = ServiceAccessor()
    s.register_descriptor(XKey, StrongX)
    s.register_descriptor(YKey, StrongY)
    with pytest.raises(CyclicalServiceDependencyError):
        s.get(XKey)


def test_validate_rejects_strong_cycle():
    s = ServiceAccessor()
    s.register_descriptor(XKey, StrongX)
    s.register_descriptor(YKey, StrongY)
    with pytest.raises(CyclicalServiceDependencyError):
        s.validate()


def test_validate_passes_for_acyclic_graph():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    s.register_descriptor(Repo)
    s.validate()   # no raise


# --------------------------------------------------------------------------- #
# Configuration vs dependency
# --------------------------------------------------------------------------- #

def test_defaulted_parameter_is_configuration():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    s.register_descriptor(Configurable)
    cfg = s.get(Configurable)
    assert isinstance(cfg.db, Database)
    assert cfg.limit == 10   # default used, not injected


# --------------------------------------------------------------------------- #
# Explicit requires
# --------------------------------------------------------------------------- #

def test_explicit_requires_injects_string_key():
    s = ServiceAccessor()
    db = Database()
    s.register("db", db)   # string-keyed instance, not inferrable from an annotation

    class StringConsumer:
        def __init__(self, *, db):   # unannotated -- only resolvable via requires
            self.db = db

    s.register_descriptor(StringConsumer, requires={"db": "db"})
    assert s.get(StringConsumer).db is db


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #

def test_child_falls_through_to_parent_and_caches_at_owner():
    root = ServiceAccessor()
    root.register_descriptor(Database)
    child = root.child("child")
    child.register_descriptor(Repo)

    repo = child.get(Repo)
    assert isinstance(repo.db, Database)
    assert root.get(Database) is repo.db   # Database cached at its owning (root) scope


def test_service_resolves_dependencies_from_its_registration_scope():
    class Svc:
        def __init__(self, *, marker: Marker):
            self.marker = marker

    root = ServiceAccessor()
    root.register(Marker, "root-value")
    root.register_descriptor(Svc)

    child = root.child("child")
    child.register(Marker, "child-value")
    child.register_descriptor(Svc)   # child's own descriptor shadows root's

    # Each Svc sees the Marker from the scope it was registered in, regardless of who asks.
    assert root.get(Svc).marker == "root-value"
    assert child.get(Svc).marker == "child-value"


# --------------------------------------------------------------------------- #
# Registration / inference errors
# --------------------------------------------------------------------------- #

def test_duplicate_registration_raises():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    with pytest.raises(DuplicateRegistrationError):
        s.register_descriptor(Database)
    with pytest.raises(DuplicateRegistrationError):
        s.register(Database, Database())


def test_service_not_found_raises():
    s = ServiceAccessor()
    with pytest.raises(ServiceNotFoundError):
        s.get(Database)


def test_unannotated_required_parameter_raises():
    s = ServiceAccessor()

    class Bad:
        def __init__(self, *, dep):   # no annotation, no default, not in requires
            self.dep = dep

    with pytest.raises(ServiceError):
        s.register_descriptor(Bad)


def test_stringized_annotation_raises():
    s = ServiceAccessor()

    class Bad:
        def __init__(self, *, db: "Database"):   # stringized (forward-ref) annotation
            self.db = db

    with pytest.raises(ServiceError):
        s.register_descriptor(Bad)


def test_requires_naming_unknown_parameter_raises():
    s = ServiceAccessor()
    s.register_descriptor(Database)
    with pytest.raises(ServiceError):
        s.register_descriptor(Repo, requires={"nope": Database})


# --------------------------------------------------------------------------- #
# Provenance: names, paths, ServiceMeta, describe, self-meta edge
# --------------------------------------------------------------------------- #

def test_scope_name_and_path():
    root = ServiceAccessor()                 # default name
    sess = root.child("conversation")
    leaf = sess.child("browser")
    assert root.name == "root"
    assert sess.path == "root -> conversation"
    assert leaf.path == "root -> conversation -> browser"


def test_describe_descriptor_metadata():
    root = ServiceAccessor()
    root.register_descriptor(Database)
    root.register_descriptor(Repo)

    meta = root.describe(Repo)
    assert isinstance(meta, ServiceMeta)
    assert meta.key is Repo
    assert meta.scope_name == "root"
    assert meta.scope_path == "root"
    assert meta.kind == "descriptor"
    assert meta.strong == (Database,)   # Repo's one strong edge
    assert "Repo" in meta.source


def test_describe_instance_metadata():
    root = ServiceAccessor()
    root.register("db", Database())
    meta = root.describe("db")
    assert meta.kind == "instance"
    assert meta.scope_name == "root"


def test_describe_falls_through_to_owning_scope():
    root = ServiceAccessor()
    root.register_descriptor(Database)
    leaf = root.child("a").child("b")
    # The metadata reports the scope that actually owns the registration, not the asker's scope.
    assert leaf.describe(Database).scope_name == "root"


def test_describe_missing_key_raises():
    root = ServiceAccessor()
    with pytest.raises(ServiceNotFoundError):
        root.describe(Database)


def test_self_meta_injection_reports_registration_scope():
    root = ServiceAccessor()
    root.register_descriptor(Database)
    sess = root.child("conversation")
    sess.register_descriptor(Provenanced)

    p = sess.get(Provenanced)
    assert isinstance(p.db, Database)            # strong edge still resolves (falls through to root)
    assert isinstance(p.meta, ServiceMeta)
    assert p.meta.key is Provenanced
    assert p.meta.scope_name == "conversation"   # its OWN registration scope, not the dep's
    assert p.meta.scope_path == "root -> conversation"
    assert p.meta.strong == (Database,)


def test_self_meta_via_explicit_requires():
    root = ServiceAccessor()

    class Bare:
        def __init__(self, *, meta):   # unannotated -- self-meta requested via requires
            self.meta = meta

    root.register_descriptor(Bare, requires={"meta": ServiceMeta})
    assert root.get(Bare).meta.key is Bare


def test_not_found_error_carries_origin_scope_and_chain():
    root = ServiceAccessor()
    root.register_descriptor(Repo)   # depends on Database, which is never registered
    leaf = root.child("conversation")

    with pytest.raises(ServiceNotFoundError) as exc:
        leaf.get(Repo)
    # The miss is for Database; the search originates at Repo's owning (root) scope and names the
    # in-flight chain that needed it.
    assert exc.value.key is Database
    assert exc.value.scope == "root"
    assert exc.value.chain == (Repo,)
    assert "needed by" in str(exc.value)


def test_cycle_error_carries_scope():
    s = ServiceAccessor("only")
    s.register_descriptor(XKey, StrongX)
    s.register_descriptor(YKey, StrongY)
    with pytest.raises(CyclicalServiceDependencyError) as exc:
        s.get(XKey)
    assert exc.value.scope == "only"


def test_duplicate_registration_error_names_scope():
    s = ServiceAccessor("only")
    s.register_descriptor(Database)
    with pytest.raises(DuplicateRegistrationError) as exc:
        s.register_descriptor(Database)
    assert "scope only" in str(exc.value)
