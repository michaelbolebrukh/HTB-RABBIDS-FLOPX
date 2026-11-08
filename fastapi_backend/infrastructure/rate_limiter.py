from ipaddress import ip_address

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request
from jose import JWTError
from platform.exceptions import JWTException

from security import decode_token
from platform.enums import TokenUse
from infrastructure.settings import settings as s


def extract_user_id_from_auth_header(request: Request) -> str | None:
    """Extracts user ID from Authorization header if present"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
        try:
            payload = decode_token(token, expected_use=TokenUse.ACCESS)
            user_id = payload.get("sub")
            # Ensuring user_id is string or None:
            return str(user_id) if user_id is not None else None
        except (JWTError, JWTException):
            return None
    return None


def client_ip(request: Request) -> str:
    """The visitor's own address, not the proxy that forwarded them.

    In production the socket peer is always the platform's load balancer, so
    `request.client.host` is the SAME value for every visitor on earth and an
    IP-keyed limit degenerates into one global bucket — measured on a preview:
    nine rapid anonymous requests, no 429, the counter moved by unrelated
    traffic. That silently disarms login brute-force, password-reset and
    account-enumeration limits as much as it does anything else.

    So the address is read from X-Forwarded-For, walking from the RIGHT and
    taking the first entry that is not a private/loopback address. Direction is
    the whole security property: each proxy APPENDS, so the rightmost public
    entry is the one our infrastructure observed, while anything a client sent
    itself sits further left and is discarded. Taking the leftmost instead —
    which is what a naive read of the header does — would let a caller mint a
    fresh bucket per request by inventing an address, and exhaust a chosen
    victim's by claiming theirs.

    Left deliberately in code rather than delegated to the server's own
    forwarded-header handling: gunicorn parses that setting before uvicorn sees
    it and rejects CIDR outright (it refuses to boot), so the two disagree
    about what is even expressible. Here the rule is one function, unit-tested.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    for candidate in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        try:
            parsed = ip_address(candidate)
        except ValueError:
            continue
        if not (parsed.is_private or parsed.is_loopback or parsed.is_link_local):
            return candidate
    # No usable forwarded address (direct connection, local dev, or a proxy
    # that strips the header) — the socket peer is the best answer available:
    return get_remote_address(request)


def user_or_ip_key_func(request: Request) -> str:
    """Rate limiting key function using user ID if authenticated, otherwise IP"""
    user_id = extract_user_id_from_auth_header(request)

    if user_id: return f"user-{user_id}"

    return client_ip(request)



# How long a single call to the counter store may take before it is treated as
# unreachable. Deliberately the same bound the cache backend uses, and the
# reason it must be set explicitly is that the inherited default is calibrated
# for a client, not for a dependency on the request path:
#
# The limits library hands these options straight to `redis.from_url`, whose
# read timeout defaults to 5 seconds — which happens to be exactly the platform
# health check's own timeout, so ONE stalled call is already enough to fail a
# probe. Worse, slowapi's middleware makes that call synchronously from inside
# the event loop, so the stall is not per-request: every concurrent request on
# the worker queues behind it. A store that accepts the TCP connection and then
# stops answering (the hung-but-listening failure, not the refused one, which
# fails instantly and harmlessly) therefore serialises the whole worker into
# 5-second blocks. That is not hypothetical — it stalled production on
# 2026-08-10 until gunicorn's own worker timeout fired and the host replaced the
# instance for failing health checks. Measured against a deliberately hung
# store, this bound takes the same call from 5.01s to 0.51s, after which the
# in-memory fallback below carries the limits.
#
# What this does NOT do, since the weaker claim is the accurate one: it does not
# make an outage a non-event. slowapi marks the store dead and then RE-PROBES it
# on a widening backoff (1s, 2s, 4s … 32s), and each re-probe is a real network
# call on the request path. So a hung store costs a recurring ~0.5s stall at
# widening intervals for as long as it stays hung, rather than one stall that
# grows until the worker dies. Bounded is the win here, not eliminated; left on
# the request path deliberately, because moving it would mean hand-rolling a
# liveness thread around a third-party library's private state to save 0.5s
# against a 5s budget.
#
# MemoryStorage accepts and ignores these, so passing them unconditionally is
# safe when CACHE_URL is unset.
_STORE_TIMEOUT_SECONDS = 0.5

# Bounding the connection pool the `limits` library hands to `redis.from_url`,
# for the same reason as `_STORE_TIMEOUT_SECONDS` above: redis-py's default
# pool has no ceiling, so a stalled store lets every blocked call keep opening
# new connections until the SERVER's own limit is what finally refuses —
# amplifying a slow store into a refusing one for every client on the account.
#
# Sized against this deployment's real shape: gunicorn runs 2 workers, each
# with its own process-local pool, and there is one other independent redis
# client in this codebase (`infrastructure/cache.py`, bounded the same way).
# Worst case both clients fully open at once: 2 workers x 2 clients x 20 = 80
# connections against the Render Key Value store's 250-connection (starter
# tier) ceiling — comfortably under a third, leaving headroom for a future
# worker-count bump and any direct/admin connections.
#
# What happens when the bound is HIT is the load-bearing question, because a
# bound that makes this call WAIT would reintroduce the exact event-loop stall
# `_STORE_TIMEOUT_SECONDS` exists to fix — slowapi calls this synchronously
# from inside the event loop. Verified empirically against a live redis-server:
# a plain (non-blocking) `ConnectionPool` with `max_connections` set raises
# `redis.exceptions.MaxConnectionsError` (a `ConnectionError` subclass)
# IMMEDIATELY when exhausted, rather than blocking for a free slot — a
# `BlockingConnectionPool` would do the latter, which is why one is not used
# here. `max_connections` as a plain `storage_options` key (which the `limits`
# library forwards straight to `redis.from_url`) builds the non-blocking pool.
# `MaxConnectionsError` is a `RedisError` -> `Exception`, caught by slowapi's
# own bare `except`/`except Exception` fail-open handlers exactly like any
# other redis error — `swallow_errors=True` below already covers it, no
# separate handling needed.
_STORE_MAX_CONNECTIONS = 20

# Counters live in the shared cache store when CACHE_URL is set, making limits
# exact across workers/instances; the in-memory default keeps counters
# per-worker (effective limits scale with worker count until then).
# A dead store must never take requests down: fall back to per-worker in-memory
# counters and swallow storage errors (fail-open, same contract as the cache):
limiter = Limiter(
    key_func=user_or_ip_key_func,
    default_limits=["50/second"],
    headers_enabled=True,
    retry_after="delta-seconds",
    storage_uri=s.CACHE_URL or "memory://",
    storage_options={
        "socket_connect_timeout": _STORE_TIMEOUT_SECONDS,
        "socket_timeout": _STORE_TIMEOUT_SECONDS,
        "max_connections": _STORE_MAX_CONNECTIONS,
    },
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
