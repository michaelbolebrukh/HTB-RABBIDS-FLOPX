"""How badly a sick rate-limit counter store is allowed to hurt the app.

The tests below pin the two properties whose absence caused a real production
incident (2026-08-10). The shared counter store stopped answering while still
accepting connections — the hung-but-listening failure, which is the dangerous
one, because a refused connection fails instantly and a hung one does not fail
at all. Two properties were missing and the outage needed both:

1. The limiter's store calls ran on redis-py's inherited 5-second read timeout
   — exactly the platform health check's own timeout — and slowapi runs them
   synchronously inside the event loop, so each stall blocked the entire
   uvicorn worker rather than one request.
2. The health endpoints were not exempt from the limiter, so the platform's
   own liveness probe went through that same store. A degraded cache therefore
   read to the host as a dead service, and the instance was replaced.

Fail-open is already the intended contract for this store; what these pin is
that failure is bounded enough to ARRIVE, and that a probe is never the thing
that waits for it.

Worth being precise about what is NOT claimed, because the weaker statement is
the true one. Bounding the call does not make the app immune during an outage —
slowapi re-probes a store it has marked dead on a widening backoff, and because
the call blocks the whole event loop, an exempt probe sharing that tick can
still be delayed by up to the bound. Measured: ~0.5s, against a 5s probe
budget. The outage becomes a survivable degradation, not a non-event.
"""

import socket
import threading
import time

import pytest
import redis.exceptions
from fastapi import FastAPI
from limits.storage import storage_from_string
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from infrastructure.dependencies import get_db
from infrastructure.handlers import rate_limit_exception_handler
from infrastructure.rate_limiter import _STORE_TIMEOUT_SECONDS, limiter
from platform.routers.root import router as root_router


# Sits between the two measured outcomes rather than at either: the configured
# bound lands at ~0.51s and the inherited default this replaces at ~5.01s, so a
# regression to the default fails here while a slow machine does not.
BOUNDED_ENOUGH_SECONDS = 2.0


@pytest.fixture
def hung_server():
    """A TCP listener that accepts connections and then says nothing at all.

    This is the failure mode being defended against, reproduced rather than
    mocked: a mocked store raises when told to, which is precisely the
    behaviour that was never in question.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept_and_ignore():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            accepted.append(conn)  # Held open, never written to.

    thread = threading.Thread(target=accept_and_ignore, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in accepted:
            conn.close()
        listener.close()


def test_store_call_gives_up_faster_than_a_health_check(hung_server):
    """A hung store must give up well inside the health probe's own timeout."""
    host, port = hung_server
    storage = storage_from_string(
        f"redis://{host}:{port}",
        socket_connect_timeout=_STORE_TIMEOUT_SECONDS,
        socket_timeout=_STORE_TIMEOUT_SECONDS,
    )

    started = time.monotonic()
    # The specific exception matters. A bare `Exception` would also be satisfied by a
    # misconfiguration that fails instantly, which passes the elapsed-time assertion for
    # entirely the wrong reason — measured: a typo'd option raises TypeError in ~0s.
    with pytest.raises(redis.exceptions.TimeoutError):
        storage.incr("some-limit-key", expiry=60)
    elapsed = time.monotonic() - started

    assert elapsed < BOUNDED_ENOUGH_SECONDS, (
        f"store call took {elapsed:.1f}s to give up; this call blocks the event "
        "loop, so anything near the 5s health-check timeout takes every "
        "concurrent request on the worker down with it"
    )


def test_the_real_limiter_carries_the_bound_not_just_the_constant(hung_server):
    """The bound has to be on the limiter the app actually uses.

    Separate from the test above on purpose. That one proves 0.5 seconds is an
    effective bound; this one proves the production `Limiter` is really carrying
    it — building the storage from `limiter`'s own `storage_options`, so a typo'd
    key, a dropped dict, or a renamed kwarg fails here instead of shipping. The
    constant being right is worth nothing if it never reaches the client.

    Note that in the test environment CACHE_URL is unset, so the live limiter's
    storage is in-memory and cannot be timed directly — the options dict is the
    only part of the real object that a redis deployment would exercise.
    """
    host, port = hung_server
    storage = storage_from_string(f"redis://{host}:{port}", **limiter._storage_options)

    started = time.monotonic()
    # Must be a TIMEOUT, not merely "something went wrong": a renamed or typo'd option
    # reaches redis-py as an unexpected kwarg and raises TypeError immediately, which a
    # looser assertion would happily accept as proof of a working bound.
    with pytest.raises(redis.exceptions.TimeoutError):
        storage.incr("some-limit-key", expiry=60)
    elapsed = time.monotonic() - started

    assert elapsed < BOUNDED_ENOUGH_SECONDS, (
        f"the limiter's own storage_options left the call unbounded at {elapsed:.1f}s "
        "— the timeout constant is not reaching the client the app builds"
    )


def _app_wired_like_main() -> FastAPI:
    """The real root router behind the real limiter middleware, wired in main.py's
    order, so what passes here says something about production.

    `/readyz` needs a working session to answer 200, so a real in-memory SQLite one
    is supplied — its `SELECT 1` is the readiness check's actual query. Only the
    database is substituted; the middleware, the router and the limiter singleton
    are the production objects, which is where this test earns its keep.
    """
    engine = create_engine("sqlite:///:memory:")
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(root_router)
    app.dependency_overrides[get_db] = override_get_db

    @app.get("/ordinary")
    def ordinary():
        return "OK"

    return app


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_endpoints_do_not_go_through_the_limiter(path):
    """Neither probe may consult the counter store.

    Read through the rate-limit headers: the limiter injects them on every
    route it actually evaluates, so their absence on a probe — while an
    ordinary route on the same app carries them — is the observable proof that
    the store was never touched.

    Both paths are parametrized rather than testing `/healthz` alone, because
    `/readyz` is the one the platform actually probes, and it was the endpoint
    whose store-gated latency got the instance replaced. Exempting only the
    endpoint nobody probes would fix nothing while looking fixed.
    """
    client = TestClient(_app_wired_like_main())

    ordinary = client.get("/ordinary")
    assert ordinary.status_code == 200
    assert "X-RateLimit-Limit" in ordinary.headers, (
        "the ordinary route is the control: if it is not being rate limited, "
        "this test cannot tell exemption apart from a disabled limiter"
    )

    probe = client.get(path)
    assert probe.status_code == 200
    assert "X-RateLimit-Limit" not in probe.headers
