"""The warm snapshot behind every FLOPX read (B-PERF): one background refresher per process, and the
composed-snapshot store the terminal's reads answer from.

Why: a read used to wait on live upstream fetches whenever an adapter's cache entry had expired
(every five minutes for most sources), and on a cold process on all of them at once; measured on
the Starter instance at about 5.4 s for /flopx/pressure. Now:

- `start()` (the product startup task, so once in every gunicorn worker; a pid guard makes a forked
  child start its own) switches `sources._http` into serving mode and starts a daemon thread. From
  then on a request thread never makes an upstream call: each adapter answers from the newest answer
  this process holds, or reports `warming` before the first one lands (the read falls back to the
  sample tier, labelled sample, and lists the source as unavailable with that reason).
- The thread first asks every seed source once (the calls the overview, feed and companies reads
  make, already filtered by the licence table: an `off` source is never called), then every
  TICK_SECONDS fetches whatever `_http.refresh_due()` says is due: each request shape on its own TTL,
  with the existing per-host spacing and Retry-After blocks.
- `composed(key, compose, db)` is the terminal's seam. Serving: the newest composition of `key` is
  returned at once, even if older than SNAPSHOT_TTL_SECONDS, and the refresher recomposes it with
  its own database session (a stale-while-revalidate read); only the very first read of a key
  composes inline, which costs CPU and the archive, never an upstream. Not serving (tests, scripts):
  it simply calls `compose(db)`, so behaviour there is exactly as before.

Truthfulness: an answer keeps the time its source gave it (observedAt), and a composition carries
its own `generatedAt` from when it was composed; serving an older composition changes neither.

Snapshots are held in the process, not the platform cache, for two reasons the caching rule's
memory backend cannot meet: stale-while-revalidate needs the expired value (the cache API drops
it), and the memory backend deep-copies every read, which for a composed read of several hundred
kilobytes costs more than the composition this replaces. Entries are bounded (MAX_SNAPSHOTS, LRU by
last read), every one has a hard age ceiling (SNAPSHOT_CEILING_SECONDS, past which a read
recomposes inline), and nothing here is needed for correctness: with the refresher stopped every
read composes as it always did. Values are shared read-only between requests: callers get a
shallow copy of a dict and must not mutate nested objects (the domain types are frozen dataclasses).
"""

from __future__ import annotations

import logging
import os
import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from product.flopx.sources import _http
from product.flopx.tools import alpaca_load, getdeploying_load

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
SNAPSHOT_TTL_SECONDS = 120         # a composition older than this is recomposed in the background
SNAPSHOT_CEILING_SECONDS = 900     # ... and one older than this is never served (recomposed inline)
RECOMPOSE_MIN_GAP_SECONDS = 60     # at most one recompose sweep per gap, whatever changed (CPU on a 0.5-CPU instance)
BOOT_QUIET_SECONDS = 90            # no sweep this long after the warm-up, so a boot is one CPU burst, not three
SWEEP_MAX_SNAPSHOTS = 8            # a sweep recomposes at most this many (oldest first); the rest wait for the next gap
WARM_BUDGET_SECONDS = 8.0          # the boot-time pre-composition stops after this, the rest composes on first read
SNAPSHOT_IDLE_DROP_SECONDS = 1800  # a key nobody read for this long is no longer recomposed
MAX_SNAPSHOTS = 160


@dataclass
class _Snapshot:
    key: str
    compose: Callable[[Any], Any]
    value: Any
    composed_at: float                # monotonic
    generation: int                   # upstream generation it was composed under
    read_at: float = 0.0
    ttl: float = SNAPSHOT_TTL_SECONDS
    follows_upstream: bool = True     # recomposed when an upstream answer changes, not only on age
    lock: threading.Lock = field(default_factory=threading.Lock)


_snapshots: dict[str, _Snapshot] = {}
_snap_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_generation = 0                       # bumped whenever an upstream answer changes
_state: dict[str, Any] = {"pid": None, "thread": None, "stop": None, "session_factory": None, "seed": None}
_state_lock = threading.Lock()


def running() -> bool:
    thread = _state["thread"]
    return _state["pid"] == os.getpid() and thread is not None and thread.is_alive()


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


def _key_lock(key: str) -> threading.Lock:
    with _snap_lock:
        return _key_locks.setdefault(key, threading.Lock())


def _store(key: str, compose: Callable[[Any], Any], value: Any, generation: int, ttl: float, follows_upstream: bool) -> None:
    now = time.monotonic()
    with _snap_lock:
        if key not in _snapshots and len(_snapshots) >= MAX_SNAPSHOTS:
            oldest = min(_snapshots.values(), key=lambda s: s.read_at)
            _snapshots.pop(oldest.key, None)
        _snapshots[key] = _Snapshot(key=key, compose=compose, value=value, composed_at=now, generation=generation, read_at=now,
                                    ttl=ttl, follows_upstream=follows_upstream)


def composed(key: str, compose: Callable[[Any], Any], db: Any, *, ttl: float = SNAPSHOT_TTL_SECONDS,
             follows_upstream: bool = True) -> Any:
    """The terminal's read seam; see the module docstring. `compose(db)` must be user-independent.
    `ttl` is the age after which the refresher recomposes it; `follows_upstream=False` is for a read
    whose result no live answer can change (the evaluation harness scores closes up to the last
    close, never today), so it is recomposed on age alone and not after every upstream answer."""
    if not running():
        return compose(db)
    ceiling = max(SNAPSHOT_CEILING_SECONDS, 2 * ttl)
    now = time.monotonic()
    snap = _snapshots.get(key)
    if snap is not None and now - snap.composed_at < ceiling:
        snap.read_at = now
        return _copy(snap.value)
    with _key_lock(key):   # one inline composition per key; a concurrent reader waits for it, not for an upstream
        snap = _snapshots.get(key)
        if snap is not None and time.monotonic() - snap.composed_at < ceiling:
            snap.read_at = time.monotonic()
            return _copy(snap.value)
        generation = _generation
        if sweep_memo() is None:
            with sweep():
                value = compose(db)
        else:
            value = compose(db)
        _store(key, compose, value, generation, ttl, follows_upstream)
        return _copy(value)


_sweep_local = threading.local()


def sweep_memo() -> dict | None:
    """The memo of the sweep running on this thread (None outside a sweep): the terminal keeps one
    build of its inferred-state inputs per session there, so a sweep of thirty reads builds them once."""
    return getattr(_sweep_local, "memo", None)


class sweep:
    """Marks a recompose sweep (or the boot warm-up) on the current thread; request threads never see it."""

    def __enter__(self):
        _sweep_local.memo = {}
        return self

    def __exit__(self, *exc):
        _sweep_local.memo = None
        return False


def snapshot_age(key: str) -> float | None:
    snap = _snapshots.get(key)
    return None if snap is None else time.monotonic() - snap.composed_at


def recompose_due(session_factory: Callable[[], Any] | None) -> int:
    """Recomposes every recently read snapshot that is older than its TTL or predates the newest
    upstream answer, all with one session and one sweep memo (the inferred-state inputs are built
    once per sweep, see `sweep_memo`); returns how many were recomposed."""
    now = time.monotonic()
    with _snap_lock:
        for key in [k for k, s in _snapshots.items() if now - s.read_at > SNAPSHOT_IDLE_DROP_SECONDS]:
            _snapshots.pop(key, None)
        due = [s for s in _snapshots.values() if (s.follows_upstream and s.generation != _generation) or now - s.composed_at >= s.ttl]
        due.sort(key=lambda s: s.composed_at)
        due = due[:SWEEP_MAX_SNAPSHOTS]
    if not due:
        return 0
    done = 0
    db = session_factory() if session_factory else None
    try:
        with sweep():
            for snap in due:
                generation = _generation
                try:
                    value = snap.compose(db)
                except Exception:
                    logger.warning("flopx snapshot %s not recomposed; the previous one stays", snap.key, exc_info=True)
                    if db is not None:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                    continue
                with _snap_lock:
                    current = _snapshots.get(snap.key)
                    if current is not None:
                        _snapshots[snap.key] = _Snapshot(key=snap.key, compose=snap.compose, value=value, composed_at=time.monotonic(),
                                                         generation=generation, read_at=current.read_at, ttl=snap.ttl,
                                                         follows_upstream=snap.follows_upstream)
                done += 1
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return done


def note_upstream_change() -> None:
    global _generation
    with _snap_lock:
        _generation += 1


def reset() -> None:
    """Forgets every snapshot (tests)."""
    global _generation
    with _snap_lock:
        _snapshots.clear()
        _key_locks.clear()
        _generation = 0


def _default_session_factory():
    from infrastructure.database import SessionLocal
    return SessionLocal


def _default_seed() -> None:
    """Asks every seed source once, in serving mode (so each call only registers its job and
    returns at once); the refresher then fetches them all. The call lists are the terminal's, so
    the licence table applies exactly as on a read."""
    from product.flopx import terminal
    from product.flopx.sources import sec
    data = terminal._data()
    tickers = list(data.companies)
    calls = [*terminal._offer_calls(), *terminal._market_calls(), *terminal._news_calls(with_filings=True),
             *terminal._energy_calls(), *terminal._facility_calls(), *terminal._close_calls(tickers), sec.fetch_frame]
    for ticker, company in data.companies.items():
        if company.cik:
            calls += [(lambda c=company.cik, t=ticker: sec.fetch_fundamentals(c, t)), (lambda c=company.cik, t=ticker: sec.fetch_filings(c, t))]
    before = set(_http.seed_keys())
    with _http._jobs_lock:
        known = set(_http._jobs)
    for call in calls:
        try:
            call()
        except Exception:
            logger.warning("flopx refresher seed call raised", exc_info=True)
    with _http._jobs_lock:
        added = set(_http._jobs) - known
    _http.mark_seed(added | before)


def warm_core(db: Any, *, budget_seconds: float = WARM_BUDGET_SECONDS) -> int:
    """Composes the reads the frontend asks for at boot, with the arguments the router passes, so the
    first burst of requests finds a snapshot instead of composing thirty reads at once. Runs in
    serving mode, so every adapter answers at once (live where this process already holds an answer,
    else `warming` and the sample tier, labelled so); stops after `budget_seconds`. Returns how many
    reads were composed."""
    from product.flopx import terminal as t
    tickers = list(t._data().companies)
    reads: list[Callable[[], Any]] = [
        lambda: t.overview(db, None), lambda: t.anomalies_view(db, None, None, None), lambda: t.pressure_grid(db, None, None),
        lambda: t.regions(), lambda: t.feed(db, None, None, None, None, None), lambda: t.markets(db), lambda: t.indices(db),
        lambda: t.effective_supply_view(db, None), lambda: t.infra_energy(db), lambda: t.companies(db),
        lambda: t.offers(db, None, None, None, None, None), lambda: t.infra_grid(db), lambda: t.infra_projects(None, None),
        lambda: t.facilities(None, None), lambda: t.factors(), *[(lambda tk=tk: t.company(db, tk)) for tk in tickers],
        # The evaluation reads (the harness over eight tickers' closes, the costliest compositions)
        # are not warmed: on the 512 MiB instance the boot peak is warm-up's transient rows stacked
        # on the seed's parsed upstream bodies (measured on Render 2026-11-08 05:13Z: the container
        # was killed about a minute after startup, before the first trim), so the two heaviest reads
        # compose on their first request instead, on an otherwise idle and trimmed process.
    ]
    started, done = time.monotonic(), 0
    for read in reads:
        if time.monotonic() - started > budget_seconds:
            break
        before = rss_mb()
        try:
            read()
            done += 1
        except Exception:
            logger.warning("flopx warm composition failed; that read composes on first request", exc_info=True)
        # Each warm read leaves transient rows behind; hand them back before the next one so the
        # boot peak is one read's transient, not the sum of twenty-five.
        gc.collect()
        release_freed_memory()
        logger.info("flopx mem: warm read %d rss %.0f -> %.0f MB", done, before, rss_mb())
    return done




def lower_thread_priority(nice: int = 15) -> None:
    """Makes the calling background thread yield the CPU to request threads and the health check.
    On the 0.5-CPU Starter instance the boot seed, the warm-up and the first sweep saturated the
    process for a minute or two after every boot; the platform's readiness probe then timed out and
    the container was replaced, over and over (measured 2026-11-08 05:13Z to 05:52Z: worker RSS
    flat at 235 MB, database idle, no OOM event, a restart one to four minutes after each start).
    Linux schedules threads individually, so the thread's own id takes the nice value. Fail-soft."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except Exception:   # not Linux, or not permitted: the thread keeps the default priority
        pass

def rss_mb() -> float:
    """This process's resident set in MB from /proc (0.0 where /proc is absent), for the memory log
    lines that let the production boot and sweep peaks be read from the service log (2026-11-08)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * 4096 / 1048576
    except Exception:
        return 0.0

def release_freed_memory() -> None:
    """Hands the heap pages a sweep freed back to the operating system (B-MEM).

    A recompose sweep allocates and frees transient rows by the hundred thousand (archive reads materialise
    every point with its provenance; measured locally with the two-year power archive: tracemalloc peak
    225 MB above the live set in one sweep) on this thread. glibc keeps the freed pages in the thread's
    arena, so without this call the worker's PSS ratchets up sweep by sweep (measured: 313 MB to 411 MB
    over fourteen minutes of sweeps with nothing retained) towards the 512 MiB instance cap. `malloc_trim`
    returns whole free pages from every arena; it costs milliseconds and changes nothing a reader sees.
    Fail-soft: on a C library without it (musl) this is a no-op."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:   # not glibc, or no shared libc to load: nothing to release
        pass


def _run(stop: threading.Event, session_factory, seed: Callable[[], None], warm: bool = True) -> None:
    last_compose = 0.0
    try:
        lower_thread_priority()
        with ThreadPoolExecutor(max_workers=_http.REFRESH_POOL, thread_name_prefix="flopx-refresh", initializer=lower_thread_priority) as pool:
            logger.info("flopx mem: refresher start rss %.0f MB", rss_mb())
            try:
                seed()
            except Exception:
                logger.warning("flopx refresher seed failed; jobs register on first read instead", exc_info=True)
            logger.info("flopx mem: after seed rss %.0f MB", rss_mb())
            if warm:
                db = session_factory() if session_factory else None
                try:
                    with sweep():
                        warm_core(db)
                finally:
                    if db is not None:
                        db.close()
                    release_freed_memory()
            last_mem_log, last_mem = time.monotonic(), rss_mb()
            logger.info("flopx mem: after warm rss %.0f MB", last_mem)
            if warm:
                # The first sweep waits: right after boot every seed answer arrives within seconds and
                # would mark every snapshot due at once, on top of the warm-up just paid for.
                last_compose = time.monotonic() - RECOMPOSE_MIN_GAP_SECONDS + BOOT_QUIET_SECONDS
            while not stop.is_set():
                try:
                    now_mem = rss_mb()
                    if abs(now_mem - last_mem) >= 12 or time.monotonic() - last_mem_log >= 30:
                        logger.info("flopx mem: rss %.0f MB (threads %d)", now_mem, threading.active_count())
                        last_mem_log, last_mem = time.monotonic(), now_mem
                    if _http.refresh_due(pool):
                        note_upstream_change()
                    now = time.monotonic()
                    if now - last_compose >= RECOMPOSE_MIN_GAP_SECONDS:
                        count = recompose_due(session_factory)
                        last_compose = time.monotonic()
                        if count:
                            logger.info("flopx refresher recomposed %d snapshots in %.0f ms", count, (last_compose - now) * 1000)
                            release_freed_memory()
                    # The 2026 closes: once a day across workers, eight requests at most (alpaca_load gates and fails soft).
                    if alpaca_load.refresh_if_due(session_factory) is not None:
                        note_upstream_change()
                    # GetDeploying's weekly GPU rental history: once an ISO week across workers, one request (gated and fail-soft in the loader).
                    if getdeploying_load.refresh_if_due(session_factory) is not None:
                        note_upstream_change()
                except Exception:
                    logger.warning("flopx refresher tick failed", exc_info=True)
                stop.wait(TICK_SECONDS)
    finally:
        # A dead refresher must not leave readers on answers nobody refreshes: they fetch again.
        if _state.get("stop") is stop:
            _http.serve_from_snapshots(False)


def start(_db: Any = None, *, force: bool = False, session_factory: Callable[[], Any] | None = None,
          seed: Callable[[], None] | None = None, warm: bool = True) -> bool:
    """Starts this process's refresher (idempotent per process). Refuses under pytest unless
    `force` (a test must never reach the network); returns whether a thread was started."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not force:
        return False
    with _state_lock:
        if running():
            return False
        stop = threading.Event()
        factory = session_factory or _default_session_factory()
        thread = threading.Thread(target=_run, args=(stop, factory, seed or _default_seed, warm), name="flopx-refresher", daemon=True)
        _state.update(pid=os.getpid(), thread=thread, stop=stop, session_factory=factory)
        _http.serve_from_snapshots(True)
        thread.start()
        logger.info("FLOPX refresher started in pid %s", os.getpid())
        return True


def stop(timeout: float = 5.0) -> None:
    with _state_lock:
        event, thread = _state.get("stop"), _state.get("thread")
        _state.update(pid=None, thread=None, stop=None)
    if event is not None:
        event.set()
    _http.serve_from_snapshots(False)
    if thread is not None:
        thread.join(timeout)
