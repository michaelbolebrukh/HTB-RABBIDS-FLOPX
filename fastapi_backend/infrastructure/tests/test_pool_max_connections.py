"""The two redis connection pools this backend builds must both be bounded.

redis-py's default pool has no ceiling — under a stall, every blocked call can
leave a connection checked out while new calls keep opening more, until the
STORE's own connection ceiling is what finally refuses. That amplifies a slow
store into a refusing one for every client on the account (the 2026-08-10
incident's failure mode, one layer further).

`max_connections=` builds redis-py's plain (non-blocking) `ConnectionPool`
rather than a `BlockingConnectionPool`, and that choice is the point being
tested, not an aside: `ConnectionPool.make_connection()` raises
`MaxConnectionsError` (a `ConnectionError` subclass) the instant the counter
is exceeded — it never attempts a socket, so exhaustion is provably a
same-tick raise rather than a wait for a freed slot. A bound that waited would
reintroduce the exact event-loop stall the socket timeouts already fix (see
`rate_limiter.py`'s own `_STORE_TIMEOUT_SECONDS` comment). Because the check is
purely a counter (`_created_connections >= max_connections`, checked before any
socket is opened — see `redis.connection.ConnectionPool.make_connection`),
these tests need no live redis server: exhausting the counter and observing
the raise never reaches the network.
"""

import time

import pytest
import redis.exceptions

from infrastructure.cache import RedisBackend
from infrastructure.rate_limiter import _STORE_MAX_CONNECTIONS, limiter


# Generous relative to the ~0ms this actually takes; only rules out the pool
# having fallen back to blocking-for-a-slot behaviour.
NON_BLOCKING_ENOUGH_SECONDS = 0.25


def _exhaust_and_time_next_call(pool):
    """Fills a connection pool's counter to its ceiling, then times one more."""
    for _ in range(pool.max_connections):
        pool.make_connection()

    started = time.monotonic()
    with pytest.raises(redis.exceptions.MaxConnectionsError):
        pool.get_connection()
    return time.monotonic() - started


def test_cache_backend_pool_is_bounded_and_rejects_without_blocking():
    """The real client `_build_cache()` would construct carries the bound.

    Built from `RedisBackend` itself (not a hand-rolled `redis.Redis.from_url`
    call) so a dropped or renamed `max_connections=` kwarg at the construction
    site fails here instead of shipping — reading `RedisBackend._MAX_CONNECTIONS`
    alone would only prove the constant exists, not that it reaches the client.
    """
    # redis.Redis.from_url does not connect eagerly, so no real store is needed
    # to construct the client:
    backend = RedisBackend("redis://127.0.0.1:1/0")
    pool = backend._client.connection_pool

    # Equality, not just "is bounded": redis-py 8.x's own inherited default is
    # 100 (not unbounded, in this pinned version), which would still satisfy a
    # bare ">0" check while leaving the wiring silently dropped. 2 workers x 2
    # clients x 100 = 400 already exceeds the 250-connection store ceiling, so
    # the inherited default is not a safe fallback here either way.
    assert pool.max_connections == RedisBackend._MAX_CONNECTIONS, (
        f"the real client's pool reports max_connections={pool.max_connections}, "
        f"not RedisBackend._MAX_CONNECTIONS ({RedisBackend._MAX_CONNECTIONS}) — "
        "the bound is not reaching the client the app constructs"
    )

    elapsed = _exhaust_and_time_next_call(pool)

    assert elapsed < NON_BLOCKING_ENOUGH_SECONDS, (
        f"pool exhaustion took {elapsed:.3f}s to raise; a bounded pool that "
        "BLOCKS on exhaustion (a BlockingConnectionPool) would reintroduce the "
        "event-loop stall the socket timeouts exist to prevent"
    )


def test_rate_limiter_storage_options_carry_the_bound():
    """The bound must be in `storage_options`, not just defined as a constant.

    Mirrors `test_the_real_limiter_carries_the_bound_not_just_the_constant` in
    `test_rate_limit_store_isolation.py`: a typo'd key or a dropped entry in
    the dict the app actually passes to `Limiter(...)` would leave the real
    client unbounded while this constant looked correct in isolation.
    """
    assert limiter._storage_options.get("max_connections") == _STORE_MAX_CONNECTIONS


def test_rate_limiter_real_storage_pool_rejects_without_blocking():
    """Builds storage the way `limits` does internally, from the limiter's own
    options, and proves exhaustion raises immediately rather than waiting.
    """
    from limits.storage import storage_from_string

    storage = storage_from_string(
        "redis://127.0.0.1:1", **limiter._storage_options
    )
    pool = storage.storage.connection_pool

    assert pool.max_connections == _STORE_MAX_CONNECTIONS

    elapsed = _exhaust_and_time_next_call(pool)

    assert elapsed < NON_BLOCKING_ENOUGH_SECONDS, (
        f"the limiter's real storage pool took {elapsed:.3f}s to reject an "
        "over-budget connection; it must raise immediately, not block"
    )


def test_worker_and_client_count_stay_under_the_store_ceiling():
    """Pins the arithmetic the bound was chosen from, so a future change to
    worker count or either pool's ceiling has to consciously revisit the sum
    rather than silently drifting past the store's connection limit.

    Values restated independently of the constants under test (not derived
    from them) so this cannot pass merely by echoing whatever the constants
    currently say.
    """
    from infrastructure.cache import RedisBackend as _RB

    gunicorn_workers = 2
    render_kv_starter_ceiling = 250

    worst_case_total = gunicorn_workers * (_RB._MAX_CONNECTIONS + _STORE_MAX_CONNECTIONS)

    assert worst_case_total < render_kv_starter_ceiling, (
        f"{worst_case_total} connections at full pool exhaustion would meet or "
        f"exceed the store's {render_kv_starter_ceiling}-connection ceiling"
    )
    # Headroom is the actual point of the bound, not just staying under 250 —
    # leave room for a worker-count increase and any direct/admin connections:
    assert worst_case_total <= render_kv_starter_ceiling // 2, (
        f"{worst_case_total} connections leaves less than half the store's "
        "ceiling as headroom"
    )
