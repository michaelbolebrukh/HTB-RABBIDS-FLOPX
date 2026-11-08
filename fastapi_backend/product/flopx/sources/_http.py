"""The one HTTP seam every live adapter uses: cached, polite, fail-soft, mockable.

`fetch_json()` and `fetch_text()` make exactly one request, with a 5 s timeout
and a declared User-Agent, and return either a `Fetched` (payload plus the
response `Date` header, which is the observation time for sources that carry
none of their own) or a `Failure` (a reason token). They never raise into a
caller: HTTP errors, timeouts, bad JSON and oversized bodies all become a
`Failure` logged at warning. Nothing secret is ever logged (no headers, no
query strings beyond the host and path).

Both outcomes are cached through the platform cache with `tags=None` (nothing
in the database invalidates an upstream feed; the TTL is the staleness bound).
A success lives for the adapter's TTL; a failure lives for `FAILURE_TTL_SECONDS`
so a flapping upstream is not hammered but a recovered one is noticed quickly.

Rate limits are honoured per host, not per request. A 429 or 503 with a
`Retry-After` header (seconds or an HTTP date) blocks every request to that host
for the stated time, capped at `RETRY_AFTER_CAP_SECONDS`, and the failure is
cached that long; a 429 without the header uses the host's default in
`RATE_LIMIT_DEFAULT_SECONDS` (Yahoo: ten minutes). Hosts in
`MIN_INTERVAL_SECONDS` are called at most once per interval across threads, so
a fan-out of eight tickers to one host is spaced rather than fired as a burst.

Tests replace `TRANSPORT` with an `httpx.MockTransport`; no test reaches the
network.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from infrastructure.cache import cache

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
FAILURE_TTL_SECONDS = 60
RETRY_AFTER_CAP_SECONDS = 3600
YAHOO_HOST = "query1.finance.yahoo.com"
# host -> seconds a 429 without Retry-After keeps the host blocked (default FAILURE_TTL_SECONDS)
RATE_LIMIT_DEFAULT_SECONDS = {YAHOO_HOST: 600}
# host -> minimum seconds between two requests to it, across every thread of this process
MIN_INTERVAL_SECONDS = {YAHOO_HOST: 1.0, "query.wikidata.org": 1.0, "api.eia.gov": 1.0}
MAX_BODY_BYTES = 4 * 1024 * 1024
USER_AGENT = "FLOPX research terminal hello@flopx.vercel.app"
DATE_HEADER_NOTE = "observedAt taken from the response Date header"

# Tests set this to an httpx.MockTransport; None means the real network.
TRANSPORT: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class Fetched:
    payload: Any
    status: int
    date: datetime | None
    fetched_at: datetime
    url: str

    @property
    def observed_at(self) -> datetime:
        """The upstream's Date header, else the fetch time (both UTC)."""
        return self.date or self.fetched_at


@dataclass(frozen=True)
class Failure:
    reason: str
    ttl_seconds: int = FAILURE_TTL_SECONDS


class _Short(Exception):
    """Carries a Failure out of the cache loader so it can be stored with a short TTL."""

    def __init__(self, failure: Failure):
        super().__init__(failure.reason)
        self.failure = failure


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date_header(response: httpx.Response) -> datetime | None:
    raw = response.headers.get("date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_host_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_last_call: dict[str, float] = {}   # in-process spacing only; the host block lives in the platform cache


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _lock_for(host: str) -> threading.Lock:
    with _host_lock:
        return _host_locks.setdefault(host, threading.Lock())


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    """`Retry-After` as whole seconds (delta-seconds or an HTTP date), capped at an hour; None when absent or unreadable."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - (now or _now())).total_seconds())
    return max(0, min(seconds, RETRY_AFTER_CAP_SECONDS))


def _rate_limited(response: httpx.Response, host: str) -> Failure:
    stated = retry_after_seconds(response.headers.get("retry-after"))
    if stated is None:
        stated = RATE_LIMIT_DEFAULT_SECONDS.get(host, FAILURE_TTL_SECONDS) if response.status_code == 429 else FAILURE_TTL_SECONDS
    ttl = max(FAILURE_TTL_SECONDS, stated)
    failure = Failure(f"http-{response.status_code}", ttl_seconds=ttl)
    # The block is shared through the platform cache (every worker on Redis), keyed by host:
    key = _block_key(host)
    cache.delete(key)
    cache.get_or_set(key, lambda: failure, ttl_seconds=ttl, tags=None)
    return failure


def _block_key(host: str) -> str:
    return f"flopx:http:blocked:{host}"


def _host_block(host: str) -> Failure | None:
    """The failure that blocked this host, while its Retry-After runs; None otherwise. (A miss stores
    None for one second, the only read the cache API offers; a new block deletes it first.)"""
    found = cache.get_or_set(_block_key(host), lambda: None, ttl_seconds=1, tags=None)
    return found if isinstance(found, Failure) else None


def _where(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _admit(host: str) -> None:
    """Waits for the host's spacing slot, then refuses (raises `_Short`) while the host is blocked. Runs
    under the host lock; the block is checked AFTER the wait, so a caller queued behind a refused
    request sees the refusal instead of firing anyway (measured on a live run: the second ticker went out)."""
    gap = MIN_INTERVAL_SECONDS.get(host)
    if gap:
        wait = _last_call.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    blocked = _host_block(host)
    if blocked is not None:
        # The host told us when to come back; every caller waits that long, not only the one that asked.
        raise _Short(Failure(blocked.reason, ttl_seconds=FAILURE_TTL_SECONDS))
    if gap:
        _last_call[host] = time.monotonic()


def _request(source: str, method: str, url: str, *, params: dict | None, headers: dict | None, json: Any, as_json: bool,
             reduce: Callable[[Any], Any] | None = None) -> Fetched:
    host = _host(url)
    if host in MIN_INTERVAL_SECONDS:
        # A spaced host is asked one request at a time, the response included, so a 429 is seen by the next caller.
        with _lock_for(host):
            _admit(host)
            return _send(source, method, url, host, params=params, headers=headers, json=json, as_json=as_json, reduce=reduce)
    with _lock_for(host):
        _admit(host)
    return _send(source, method, url, host, params=params, headers=headers, json=json, as_json=as_json, reduce=reduce)


def _send(source: str, method: str, url: str, host: str, *, params: dict | None, headers: dict | None, json: Any, as_json: bool,
          reduce: Callable[[Any], Any] | None = None) -> Fetched:
    sent = {"User-Agent": USER_AGENT, "Accept": "application/json" if as_json else "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"}
    sent.update(headers or {})
    try:
        # A mock transport must see every request: with trust_env left on, httpx would route
        # https URLs through a proxy mount built from the environment and bypass the mock.
        with httpx.Client(transport=TRANSPORT, trust_env=TRANSPORT is None, timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.request(method, url, params=params, headers=sent, json=json)
    except httpx.TimeoutException:
        logger.warning("%s: timeout after %.0fs (%s)", source, TIMEOUT_SECONDS, _where(url))
        raise _Short(Failure("timeout"))
    except httpx.HTTPError as exc:
        logger.warning("%s: %s (%s)", source, exc.__class__.__name__, _where(url))
        raise _Short(Failure("network-error"))
    if response.status_code in (429, 503):
        failure = _rate_limited(response, host)
        logger.warning("%s: HTTP %s (%s), host paused %ss", source, response.status_code, _where(url), failure.ttl_seconds)
        raise _Short(failure)
    if response.status_code >= 400:
        logger.warning("%s: HTTP %s (%s)", source, response.status_code, _where(url))
        raise _Short(Failure(f"http-{response.status_code}"))
    if len(response.content) > MAX_BODY_BYTES:
        logger.warning("%s: body over %d bytes (%s)", source, MAX_BODY_BYTES, _where(url))
        raise _Short(Failure("body-too-large"))
    if as_json:
        try:
            payload = response.json()
        except ValueError:
            logger.warning("%s: response is not JSON (%s)", source, _where(url))
            raise _Short(Failure("bad-json"))
        if reduce is not None:
            # B-MEM: the held answer is the slice the adapter reads, not the whole body. A served process keeps every
            # answer for its lifetime (`_last_good`), and a parsed SEC companyfacts body measured 2 to 17 MB of Python
            # objects per filer against a few hundred kilobytes for the tags the adapter keeps.
            payload = reduce(payload)
    else:
        payload = response.text
    return Fetched(payload=payload, status=response.status_code, date=_date_header(response), fetched_at=_now(), url=str(response.url))


def _through_cache(key: str, ttl_seconds: int, load) -> Fetched | Failure:
    try:
        return cache.get_or_set(key, load, ttl_seconds=ttl_seconds, tags=None)
    except _Short as short:
        # Store the failure briefly so the upstream is not hammered while down:
        return cache.get_or_set(key, lambda: short.failure, ttl_seconds=short.failure.ttl_seconds, tags=None)


# -- background refresh (B-PERF) ------------------------------------------------------------
#
# Every request shape an adapter asks for is a JOB: its cache key, TTL and the closure that makes
# the request. Outside serving mode (tests, scripts, a process with no refresher) `_cached` fetches
# through the platform cache exactly as before. In SERVING mode, which `product.flopx.refresher`
# switches on in each served process, a request thread never touches the network: it is answered
# from the newest successful answer this process holds (`_last_good`, whose observedAt is still when
# the source answered), or, before the first answer lands, with `Failure("warming")`, which the
# adapter reports as unavailable so the read falls back to the sample tier and says so. The
# refresher thread alone calls `refresh_due()`, which fetches every job whose answer is older than
# REFRESH_AT x its TTL (or that a request just found missing), on a small pool, with the per-host
# spacing and Retry-After blocks of `_request` unchanged. A failed fetch is retried after its own
# failure TTL (a 429's Retry-After included); a job nobody has asked for in IDLE_DROP_SECONDS is
# dropped, unless it is a seed (the sources the overview reads), so rarely read shapes are not
# polled forever. A last answer older than STALE_CEILING x its TTL is no longer served: the read
# then reports the source unavailable rather than present an hours-old answer as current.

REFRESH_AT = 0.8
STALE_CEILING = 12
IDLE_DROP_SECONDS = 1800
MAX_JOBS = 400
REFRESH_POOL = 2   # 2026-11-08: was 4; each in-flight fetch holds a body of up to 4 MiB and its parsed object, and the boot seed fetches every job at once on a 512 MiB instance
WARMING = "warming"


@dataclass
class Job:
    source: str
    key: str
    ttl_seconds: int
    load: Any
    seed: bool = False
    requested_at: float = 0.0        # monotonic: the last time a reader asked for it
    answered_at: float | None = None  # monotonic: when the newest success landed
    due_at: float = 0.0              # monotonic: when the refresher should fetch it next
    last_failure: Failure | None = None
    running: bool = False


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
_last_good: dict[str, Fetched] = {}
_serving = False


def serving() -> bool:
    return _serving


def serve_from_snapshots(on: bool) -> None:
    """Switches request threads to snapshot answers (on) or back to fetching through the cache (off)."""
    global _serving
    _serving = on


def reset_jobs() -> None:
    """Forgets every job and held answer (tests)."""
    with _jobs_lock:
        _jobs.clear()
        _last_good.clear()


def _register(source: str, key: str, ttl_seconds: int, load, *, requested: bool = True) -> Job:
    now = time.monotonic()
    with _jobs_lock:
        job = _jobs.get(key)
        if job is None:
            if len(_jobs) >= MAX_JOBS:
                # The least recently requested non-seed job makes room (a keying bug degrades into drops, not growth):
                victims = sorted((j for j in _jobs.values() if not j.seed), key=lambda j: j.requested_at)
                for victim in victims[:max(1, len(_jobs) - MAX_JOBS + 1)]:
                    _jobs.pop(victim.key, None)
                    _last_good.pop(victim.key, None)
            job = _jobs[key] = Job(source=source, key=key, ttl_seconds=ttl_seconds, load=load, due_at=now)
        job.load = load   # the newest closure (a dated window moves on)
        job.ttl_seconds = ttl_seconds
        if requested:
            job.requested_at = now
        return job


def mark_seed(keys) -> None:
    with _jobs_lock:
        for key in keys:
            if key in _jobs:
                _jobs[key].seed = True


def seed_keys() -> list[str]:
    with _jobs_lock:
        return [k for k, j in _jobs.items() if j.seed]


def _remember(job: Job, result: Fetched | Failure) -> None:
    now = time.monotonic()
    with _jobs_lock:
        if isinstance(result, Fetched):
            _last_good[job.key] = result
            job.answered_at = now
            job.last_failure = None
            job.due_at = now + job.ttl_seconds * REFRESH_AT
        else:
            job.last_failure = result
            job.due_at = now + max(result.ttl_seconds, FAILURE_TTL_SECONDS)


def _serve(job: Job) -> Fetched | Failure:
    """A request thread's answer in serving mode: never a network call."""
    now = time.monotonic()
    with _jobs_lock:
        good = _last_good.get(job.key)
        age = None if job.answered_at is None else now - job.answered_at
        if age is None or age >= job.ttl_seconds:
            job.due_at = min(job.due_at, now)   # the refresher picks it up on its next tick
        if good is not None and age is not None and age <= job.ttl_seconds * STALE_CEILING:
            return good
        return job.last_failure or Failure(WARMING, ttl_seconds=0)


def _cached(source: str, key: str, ttl_seconds: int, load) -> Fetched | Failure:
    job = _register(source, key, ttl_seconds, load)
    if _serving:
        return _serve(job)
    result = _through_cache(key, ttl_seconds, load)
    _remember(job, result)
    return result


def _fetch_now(job: Job) -> bool:
    """One blocking fetch for the refresher; True when it produced a new success."""
    try:
        result: Fetched | Failure = job.load()
    except _Short as short:
        result = short.failure
    except Exception:   # a bug in a loader must not kill the refresher
        logger.warning("%s: refresh raised", job.source, exc_info=True)
        result = Failure("adapter-error")
    finally:
        with _jobs_lock:
            job.running = False
    _remember(job, result)
    return isinstance(result, Fetched)


def due_jobs(now: float | None = None) -> list[Job]:
    """Jobs to fetch now, oldest due first; drops jobs idle past IDLE_DROP_SECONDS (seeds excepted)."""
    now = time.monotonic() if now is None else now
    with _jobs_lock:
        for key in [k for k, j in _jobs.items() if not j.seed and now - j.requested_at > IDLE_DROP_SECONDS]:
            _jobs.pop(key, None)
            _last_good.pop(key, None)
        due = sorted((j for j in _jobs.values() if not j.running and j.due_at <= now), key=lambda j: j.due_at)
        for job in due:
            job.running = True
        return due


def refresh_due(pool=None) -> int:
    """Fetches every due job (blocking, on `pool` when given); returns how many produced a new answer."""
    due = due_jobs()
    if not due:
        return 0
    if pool is None or len(due) == 1:
        return sum(_fetch_now(job) for job in due)
    return sum(pool.map(_fetch_now, due))


def fetch_json(source: str, url: str, *, key: str, ttl_seconds: int, params: dict | None = None,
               headers: dict | None = None, method: str = "GET", json: Any = None,
               reduce: Callable[[Any], Any] | None = None) -> Fetched | Failure:
    """One JSON request, cached under `key` for `ttl_seconds` (failures for less); never raises. `reduce`, when
    given, is applied to the parsed body once, so what is cached and held is the slice the adapter reads."""
    return _cached(source, key, ttl_seconds, lambda: _request(source, method, url, params=params, headers=headers, json=json, as_json=True, reduce=reduce))


def fetch_text(source: str, url: str, *, key: str, ttl_seconds: int, params: dict | None = None,
               headers: dict | None = None) -> Fetched | Failure:
    """One text request (RSS/XML), cached like `fetch_json`; never raises."""
    return _cached(source, key, ttl_seconds, lambda: _request(source, "GET", url, params=params, headers=headers, json=None, as_json=False))


def epoch(seconds: float | int | None) -> datetime | None:
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def iso(text: str | None) -> datetime | None:
    if not text:
        return None
    value = str(text).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Every time leaves this module in UTC, whatever offset the upstream wrote (an RSS
    # pubDate at -05:00 is the same instant, but the wire shape is UTC everywhere else).
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rfc822(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
