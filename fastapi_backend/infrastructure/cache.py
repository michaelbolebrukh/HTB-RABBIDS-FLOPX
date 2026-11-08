"""Platform caching layer.

One `cache` singleton backed by either an in-process bounded memory store
(default, zero config) or Redis/Valkey (set CACHE_URL=redis://...). Callers
use `cache.get_or_set(...)` or the `@cached(...)` decorator; which backend
serves the call is an environment decision, never a code decision.

Invalidation model (layered, safest-first):
- Every entry has a TTL ceiling. Nothing outlives it, ever.
- Tag generations: entries record the generation counter of each tag they
  were built under; a bumped tag makes every dependent entry read as a miss
  (O(1) invalidation, no key scanning). Tags default to the DB tables the
  loader read (tracked automatically via SQLAlchemy events); pass
  `tags=None` for TTL-only caches over write-hot tables (see caching rule).
- The memory backend is additionally bounded by LRU, so a keying bug
  degrades into evictions rather than memory growth.

Failure policy is fail-open: a broken cache backend logs and degrades to
computing values uncached; it must never take the app down.

Cross-worker note: with the memory backend each gunicorn worker holds its
own cache, so a tag bump in one worker cannot evict siblings — TTL is the
staleness bound across workers. Redis makes both data and invalidation
shared. In-process per-key locks bound recomputation stampedes to at most
one concurrent recompute per worker by design.
"""

import copy
import logging
import pickle
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Iterable, Optional, TypeVar

from cachetools import LRUCache
from sqlalchemy import Table
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy import event, inspect as sa_inspect

from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Sentinel distinguishing "not cached" from a cached None:
_MISS = object()

# Tables the current cached loader has read (None = not inside a tracked loader):
_tracked_tables: ContextVar[Optional[set[str]]] = ContextVar("cache_tracked_tables", default=None)

# Per-request hit/miss events, set by the middleware and appended to by the cache:
_request_events: ContextVar[Optional[list[str]]] = ContextVar("cache_request_events", default=None)


# ============================================================================
# BACKENDS
# ============================================================================

class MemoryBackend:
    """Bounded in-process store: LRU-capped entries with per-key expiry.

    Payloads are deep-copied on both write and read so callers can never
    mutate a cached value in place (the Redis backend gets the same safety
    from its pickle round-trip).
    """

    def __init__(self, max_entries: int):
        self._entries: LRUCache = LRUCache(maxsize=max_entries)
        self._counters: dict[str, int] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Any:
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return _MISS
            expires_at, payload = item
            if time.monotonic() >= expires_at:
                self._entries.pop(key, None)
                return _MISS
            return copy.deepcopy(payload)

    def set(self, key: str, payload: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + ttl_seconds, copy.deepcopy(payload))

    def delete(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def incr(self, counter_key: str) -> None:
        with self._lock:
            self._counters[counter_key] = self._counters.get(counter_key, 0) + 1

    def get_counters(self, counter_keys: list[str]) -> dict[str, int]:
        with self._lock:
            return {k: self._counters.get(k, 0) for k in counter_keys}


class RedisBackend:
    """Redis/Valkey store; values are pickled (private, app-written-only cache).

    Errors are raised to the facade, which fails open.
    """

    # Bounding the connection pool. redis-py's default pool has NO ceiling —
    # under a stall (see rate_limiter.py's own timeout comment for the
    # production incident this guards against) every blocked call can leave a
    # connection checked out while new calls keep opening more, until the
    # SERVER's own connection ceiling is what finally says no. That turns a
    # slow store into a refusing one for every client on the account, not just
    # this one.
    #
    # The bound is sized against this deployment's actual shape: gunicorn runs
    # 2 workers, each holding one process-local pool, and there are two
    # independent redis clients in this codebase (this one and
    # rate_limiter.py's), each with its own pool. At this ceiling, the worst
    # case both clients fully open at once is 2 workers x 2 clients x 20 = 80
    # connections against the Render Key Value store's 250-connection (starter
    # tier) ceiling — comfortably under a third, leaving headroom for a future
    # worker-count increase, direct/admin connections, and the rate limiter's
    # own pool without the two competing for the same budget.
    #
    # What happens when the bound IS hit matters more than the number: redis-py's
    # plain (non-blocking) ConnectionPool.get_connection raises
    # `redis.exceptions.MaxConnectionsError` (a `ConnectionError` subclass)
    # IMMEDIATELY — it never blocks waiting for a free connection. Verified
    # empirically (held 3 live connections against max_connections=3, then
    # issued a 4th call: raised in ~0ms, not the connect/read timeout). A
    # BlockingConnectionPool would instead wait up to `timeout` for a slot,
    # which — since this call runs synchronously on the event loop — would
    # reintroduce exactly the stall this file's `socket_timeout` exists to
    # prevent. redis.Redis.from_url with a plain `max_connections=` kwarg
    # builds the non-blocking pool, which is what is used here deliberately.
    # `MaxConnectionsError` is a `RedisError` -> `Exception`, so it is caught
    # by this class's existing broad `except Exception` fail-open paths
    # (`_read`/`_write`/`delete`/`invalidate_tag` below) exactly like any other
    # redis error — no separate handling needed.
    _MAX_CONNECTIONS = 20

    def __init__(self, url: str):
        # Importing lazily so the dependency is only exercised when configured:
        import redis

        self._client = redis.Redis.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            max_connections=self._MAX_CONNECTIONS,
        )

    def get(self, key: str) -> Any:
        raw = self._client.get(key)
        if raw is None:
            return _MISS
        return pickle.loads(raw)

    def set(self, key: str, payload: Any, ttl_seconds: int) -> None:
        self._client.setex(key, ttl_seconds, pickle.dumps(payload))

    def delete(self, keys: Iterable[str]) -> None:
        keys = list(keys)
        if keys:
            self._client.delete(*keys)

    def incr(self, counter_key: str) -> None:
        self._client.incr(counter_key)

    def get_counters(self, counter_keys: list[str]) -> dict[str, int]:
        values = self._client.mget(counter_keys)
        return {
            key: int(value) if value is not None else 0
            for key, value in zip(counter_keys, values)
        }


# ============================================================================
# CACHE FACADE
# ============================================================================

class Cache:
    """Namespaced, tag-aware, fail-open cache facade over a pluggable backend."""

    AUTO = "auto"

    def __init__(self, backend: Any, namespace: str):
        self._backend = backend
        self._namespace = namespace
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    # ---- key helpers -------------------------------------------------------

    def _entry_key(self, key: str) -> str:
        return f"{self._namespace}:entry:{key}"

    def _tag_key(self, tag: str) -> str:
        return f"{self._namespace}:tag:{tag}"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    @staticmethod
    def _record_event(kind: str) -> None:
        events = _request_events.get()
        if events is not None:
            events.append(kind)

    # ---- core API ----------------------------------------------------------

    def get_or_set(
        self,
        key: str,
        loader: Callable[[], T],
        ttl_seconds: Optional[int] = None,
        tags: Any = AUTO,
    ) -> T:
        """Returns the cached value for `key`, computing and storing it on miss.

        tags=Cache.AUTO (default): tags the entry with every DB table the
        loader reads, for automatic invalidation on writes to those tables.
        tags=None: TTL-only (write-hot sources — see the caching rule).
        tags=[...]: explicit tag list.
        """
        ttl = ttl_seconds if ttl_seconds is not None else s.CACHE_DEFAULT_TTL

        cached_value = self._read(key)
        if cached_value is not _MISS:
            self._record_event("hit")
            return cached_value

        # Bounding the stampede to one concurrent recompute per worker:
        with self._lock_for(key):
            cached_value = self._read(key)
            if cached_value is not _MISS:
                self._record_event("hit")
                return cached_value

            self._record_event("miss")
            value, read_tables = self._run_loader(loader, track=tags == self.AUTO)

            if tags == self.AUTO:
                entry_tags = sorted(f"table:{table}" for table in read_tables)
            elif tags is None:
                entry_tags = []
            else:
                entry_tags = sorted(set(tags))

            self._write(key, value, entry_tags, ttl)
            return value

    def delete(self, *keys: str) -> None:
        """Evicts specific entries immediately."""
        try:
            self._backend.delete(self._entry_key(key) for key in keys)
        except Exception:
            logger.warning("Cache delete failed; continuing without cache.", exc_info=True)

    def invalidate_tag(self, tag: str) -> None:
        """Bumps a tag generation, invalidating every entry built under it."""
        try:
            self._backend.incr(self._tag_key(tag))
        except Exception:
            logger.warning("Cache tag invalidation failed; TTL remains the bound.", exc_info=True)

    def invalidate_tables(self, table_names: Iterable[str]) -> None:
        """Invalidates all entries whose loaders read any of the given tables."""
        for name in table_names:
            self.invalidate_tag(f"table:{name}")

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _run_loader(loader: Callable[[], T], track: bool) -> tuple[T, set[str]]:
        if not track:
            return loader(), set()
        token = _tracked_tables.set(set())
        try:
            value = loader()
            return value, _tracked_tables.get() or set()
        finally:
            _tracked_tables.reset(token)

    def _read(self, key: str) -> Any:
        try:
            payload = self._backend.get(self._entry_key(key))
            if payload is _MISS:
                return _MISS
            value, tag_generations = payload
            if tag_generations:
                current = self._backend.get_counters(
                    [self._tag_key(tag) for tag in tag_generations]
                )
                for tag, generation in tag_generations.items():
                    if current.get(self._tag_key(tag), 0) != generation:
                        return _MISS
            return value
        except Exception:
            logger.warning("Cache read failed; computing uncached.", exc_info=True)
            return _MISS

    def _write(self, key: str, value: Any, entry_tags: list[str], ttl: int) -> None:
        try:
            tag_generations: dict[str, int] = {}
            if entry_tags:
                current = self._backend.get_counters(
                    [self._tag_key(tag) for tag in entry_tags]
                )
                tag_generations = {
                    tag: current.get(self._tag_key(tag), 0) for tag in entry_tags
                }
            self._backend.set(self._entry_key(key), (value, tag_generations), ttl)
        except Exception:
            logger.warning("Cache write failed; continuing without cache.", exc_info=True)


def _build_cache() -> Cache:
    namespace = f"{s.APP_DOMAIN}:cache"
    if s.CACHE_URL:
        try:
            backend: Any = RedisBackend(s.CACHE_URL)
            logger.info("Cache backend: redis (%s)", s.CACHE_URL.split("@")[-1])
            return Cache(backend, namespace)
        except Exception:
            logger.warning(
                "CACHE_URL is set but the Redis backend failed to initialize; "
                "falling back to in-process memory cache.",
                exc_info=True,
            )
    return Cache(MemoryBackend(s.CACHE_MAX_MEMORY_ENTRIES), namespace)


# Module singleton, like `settings` and `SessionLocal`:
cache = _build_cache()


# ============================================================================
# DECORATOR
# ============================================================================

def cached(
    key: str | Callable[..., str],
    ttl_seconds: Optional[int] = None,
    tags: Any = Cache.AUTO,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Caches a service function's result under `key`.

    `key` is a literal string or a callable receiving the function's args
    (including the db session — ignore it in the lambda). The undecorated
    function stays reachable via `.__wrapped__` for tests.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args: Any, **kwargs: Any) -> T:
            cache_key = key(*args, **kwargs) if callable(key) else key
            return cache.get_or_set(
                cache_key,
                lambda: fn(*args, **kwargs),
                ttl_seconds=ttl_seconds,
                tags=tags,
            )

        wrapper.__wrapped__ = fn
        wrapper.__name__ = getattr(fn, "__name__", "cached")
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


# ============================================================================
# AUTOMATIC INVALIDATION (SQLAlchemy session events)
# ============================================================================

def _tables_in_statement(statement: Any) -> set[str]:
    """Collects the table names a SQLAlchemy statement touches."""
    try:
        return {
            element.name
            for element in visitors.iterate(statement, {})
            if isinstance(element, Table)
        }
    except Exception:
        return set()


def _on_do_orm_execute(orm_execute_state: Any) -> None:
    # Recording reads while a tags=AUTO loader is running:
    if orm_execute_state.is_select:
        tracked = _tracked_tables.get()
        if tracked is not None:
            tracked.update(_tables_in_statement(orm_execute_state.statement))
        return

    # Recording ORM-enabled bulk writes (e.g. query(...).delete()):
    if isinstance(orm_execute_state.statement, (Insert, Update, Delete)):
        written = orm_execute_state.session.info.setdefault("cache_written_tables", set())
        written.update(_tables_in_statement(orm_execute_state.statement))


def _on_after_flush(session: Any, flush_context: Any) -> None:
    written = session.info.setdefault("cache_written_tables", set())
    for obj in [*session.new, *session.dirty, *session.deleted]:
        try:
            for table in sa_inspect(obj).mapper.tables:
                written.add(table.name)
        except Exception:
            continue


def _on_after_commit(session: Any) -> None:
    written = session.info.pop("cache_written_tables", None)
    if written:
        cache.invalidate_tables(written)


def _on_after_rollback(session: Any) -> None:
    session.info.pop("cache_written_tables", None)


def install_cache_invalidation(session_factory: Any) -> None:
    """Wires automatic read-tracking and write-invalidation onto a session factory.

    Reads during a tags=AUTO loader tag the entry with the tables it touched;
    committed writes bump those tables' tags. Writes that bypass the ORM
    session (raw SQL, external systems) are invisible here — the TTL ceiling
    covers those.
    """
    event.listen(session_factory, "do_orm_execute", _on_do_orm_execute)
    event.listen(session_factory, "after_flush", _on_after_flush)
    event.listen(session_factory, "after_commit", _on_after_commit)
    event.listen(session_factory, "after_rollback", _on_after_rollback)


# ============================================================================
# X-CACHE MIDDLEWARE
# ============================================================================

class CacheHeaderMiddleware:
    """Stamps X-Cache: HIT|MISS on responses that touched the cache.

    HIT means every cache lookup in the request was served from cache; any
    recompute marks the response MISS. Requests that never touch the cache
    get no header.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _request_events.set([])

        async def send_with_header(message: Any) -> None:
            if message["type"] == "http.response.start":
                events = _request_events.get()
                if events:
                    verdict = b"HIT" if all(e == "hit" for e in events) else b"MISS"
                    message.setdefault("headers", []).append((b"x-cache", verdict))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_events.reset(token)
