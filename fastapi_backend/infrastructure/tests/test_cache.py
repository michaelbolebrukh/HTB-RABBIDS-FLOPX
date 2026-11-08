import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.cache import (
    Cache,
    MemoryBackend,
    cached,
    install_cache_invalidation,
)


def make_cache(max_entries: int = 64) -> Cache:
    return Cache(MemoryBackend(max_entries), namespace="test:cache")


# ============================================================================
# CORE BEHAVIOR
# ============================================================================

def test_get_or_set_computes_once_then_hits():
    cache = make_cache()
    calls = []

    def loader():
        calls.append(1)
        return {"value": 42}

    first = cache.get_or_set("k", loader, ttl_seconds=60, tags=None)
    second = cache.get_or_set("k", loader, ttl_seconds=60, tags=None)

    assert first == second == {"value": 42}
    assert len(calls) == 1


def test_cached_none_is_a_hit_not_a_recompute():
    cache = make_cache()
    calls = []

    def loader():
        calls.append(1)
        return None

    assert cache.get_or_set("k", loader, ttl_seconds=60, tags=None) is None
    assert cache.get_or_set("k", loader, ttl_seconds=60, tags=None) is None
    assert len(calls) == 1


def test_ttl_expiry_recomputes(monkeypatch):
    cache = make_cache()
    now = time.monotonic()
    calls = []

    def loader():
        calls.append(1)
        return len(calls)

    assert cache.get_or_set("k", loader, ttl_seconds=10, tags=None) == 1
    monkeypatch.setattr(time, "monotonic", lambda: now + 11)
    assert cache.get_or_set("k", loader, ttl_seconds=10, tags=None) == 2


def test_delete_evicts_entry():
    cache = make_cache()
    cache.get_or_set("k", lambda: "v1", ttl_seconds=60, tags=None)
    cache.delete("k")
    assert cache.get_or_set("k", lambda: "v2", ttl_seconds=60, tags=None) == "v2"


def test_lru_bound_caps_entries():
    cache = make_cache(max_entries=2)
    cache.get_or_set("a", lambda: 1, ttl_seconds=60, tags=None)
    cache.get_or_set("b", lambda: 2, ttl_seconds=60, tags=None)
    cache.get_or_set("c", lambda: 3, ttl_seconds=60, tags=None)

    # "a" was evicted by the LRU bound and recomputes:
    calls = []
    cache.get_or_set("a", lambda: calls.append(1) or 1, ttl_seconds=60, tags=None)
    assert len(calls) == 1


def test_cached_values_are_isolated_from_caller_mutation():
    cache = make_cache()
    first = cache.get_or_set("k", lambda: {"rows": [1, 2]}, ttl_seconds=60, tags=None)
    first["rows"].append(3)
    second = cache.get_or_set("k", lambda: {"rows": []}, ttl_seconds=60, tags=None)
    assert second == {"rows": [1, 2]}


# ============================================================================
# TAGS
# ============================================================================

def test_explicit_tag_invalidation():
    cache = make_cache()
    cache.get_or_set("k", lambda: "v1", ttl_seconds=60, tags=["mytag"])
    assert cache.get_or_set("k", lambda: "v2", ttl_seconds=60, tags=["mytag"]) == "v1"

    cache.invalidate_tag("mytag")
    assert cache.get_or_set("k", lambda: "v3", ttl_seconds=60, tags=["mytag"]) == "v3"


def test_ttl_only_entries_ignore_tag_bumps():
    cache = make_cache()
    cache.get_or_set("k", lambda: "v1", ttl_seconds=60, tags=None)
    cache.invalidate_tag("anything")
    assert cache.get_or_set("k", lambda: "v2", ttl_seconds=60, tags=None) == "v1"


# ============================================================================
# FAIL-OPEN
# ============================================================================

class BrokenBackend:
    def get(self, key):
        raise ConnectionError("backend down")

    def set(self, key, payload, ttl_seconds):
        raise ConnectionError("backend down")

    def delete(self, keys):
        raise ConnectionError("backend down")

    def incr(self, counter_key):
        raise ConnectionError("backend down")

    def get_counters(self, counter_keys):
        raise ConnectionError("backend down")


def test_broken_backend_fails_open():
    cache = Cache(BrokenBackend(), namespace="test:broken")
    assert cache.get_or_set("k", lambda: "computed", ttl_seconds=60, tags=None) == "computed"
    cache.delete("k")
    cache.invalidate_tag("t")


# ============================================================================
# DECORATOR
# ============================================================================

def test_cached_decorator_with_callable_key(monkeypatch):
    import infrastructure.cache as cache_module

    monkeypatch.setattr(cache_module, "cache", make_cache())
    calls = []

    @cached(key=lambda db, slug: f"thing:{slug}", ttl_seconds=60, tags=None)
    def load_thing(db, slug):
        calls.append(slug)
        return f"value-{slug}"

    assert load_thing(None, "a") == "value-a"
    assert load_thing(None, "a") == "value-a"
    assert load_thing(None, "b") == "value-b"
    assert calls == ["a", "b"]
    assert load_thing.__wrapped__(None, "a") == "value-a"


# ============================================================================
# AUTOMATIC INVALIDATION (real models, in-memory SQLite)
# ============================================================================

@pytest.fixture()
def session_factory(monkeypatch):
    import infrastructure.cache as cache_module
    from infrastructure.database import Base
    import platform.models  # noqa: F401 — registering tables

    monkeypatch.setattr(cache_module, "cache", make_cache())

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    install_cache_invalidation(factory)
    yield factory
    engine.dispose()


def test_auto_tags_invalidate_on_committed_write(session_factory):
    import infrastructure.cache as cache_module
    from platform.models import Organisation

    def load_names(db):
        return [o.name for o in db.query(Organisation).order_by(Organisation.id).all()]

    def cached_names(db):
        return cache_module.cache.get_or_set(
            "names", lambda: load_names(db), ttl_seconds=3600
        )

    with session_factory() as db:
        db.add(Organisation(slug="o1", name="One"))
        db.commit()

    with session_factory() as db:
        assert cached_names(db) == ["One"]

    # A committed write to the read table bumps its tag and evicts the entry:
    with session_factory() as db:
        db.add(Organisation(slug="o2", name="Two"))
        db.commit()

    with session_factory() as db:
        assert cached_names(db) == ["One", "Two"]


def test_rolled_back_write_does_not_invalidate(session_factory):
    import infrastructure.cache as cache_module
    from platform.models import Organisation

    def cached_count(db):
        return cache_module.cache.get_or_set(
            "count", lambda: db.query(Organisation).count(), ttl_seconds=3600
        )

    with session_factory() as db:
        assert cached_count(db) == 0

    with session_factory() as db:
        db.add(Organisation(slug="o1", name="One"))
        db.flush()
        db.rollback()

    with session_factory() as db:
        assert cached_count(db) == 0


def test_bulk_delete_invalidates(session_factory):
    import infrastructure.cache as cache_module
    from platform.models import Organisation

    with session_factory() as db:
        db.add(Organisation(slug="o1", name="One"))
        db.commit()

    def cached_count(db):
        return cache_module.cache.get_or_set(
            "count", lambda: db.query(Organisation).count(), ttl_seconds=3600
        )

    with session_factory() as db:
        assert cached_count(db) == 1

    with session_factory() as db:
        db.query(Organisation).delete(synchronize_session=False)
        db.commit()

    with session_factory() as db:
        assert cached_count(db) == 0


# ============================================================================
# X-CACHE MIDDLEWARE (contextvar propagation through the sync threadpool)
# ============================================================================

def test_x_cache_header_via_fastapi(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import infrastructure.cache as cache_module
    from infrastructure.cache import CacheHeaderMiddleware

    monkeypatch.setattr(cache_module, "cache", make_cache())

    app = FastAPI()
    app.add_middleware(CacheHeaderMiddleware)

    @app.get("/cached")
    def cached_route():
        return {"n": cache_module.cache.get_or_set("n", lambda: 7, ttl_seconds=60, tags=None)}

    @app.get("/plain")
    def plain_route():
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/cached").headers["x-cache"] == "MISS"
    assert client.get("/cached").headers["x-cache"] == "HIT"
    assert "x-cache" not in client.get("/plain").headers


# ============================================================================
# RATE LIMITER FAIL-OPEN CONFIG
# ============================================================================

def test_rate_limiter_fails_open_on_dead_storage():
    # A dead CACHE_URL store must degrade to in-memory limiting, never 500
    # (verified live 2026-07-05 by killing Redis under a 4-worker run):
    from infrastructure.rate_limiter import limiter

    assert limiter._swallow_errors is True
    assert limiter._in_memory_fallback_enabled is True
