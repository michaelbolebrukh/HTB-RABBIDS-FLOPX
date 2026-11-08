"""Gunicorn configuration (auto-loaded from the working directory).

Two responsibilities, both memory-oriented defaults that ship on — there are
deliberately no env vars to enable them, so every clone gets the safe behaviour
for free:

1. Worker-lifecycle log filter (master + each worker), so a graceful-shutdown
   SIGTERM (every deploy) stops surfacing as an error/alert while real worker
   deaths (OOM, timeout, boot failure) still do. See infrastructure/log_noise.py.

2. Fixed per-worker memory footprint and drift control:
   - `preload_app`, with more than one worker: importing the app once in the
     master and forking, so heavy module-level imports (the AI SDK stack,
     SQLAlchemy, Sentry) are shared copy-on-write across workers instead of
     duplicated per worker. Worker COUNT is still `WEB_CONCURRENCY` (gunicorn
     reads it natively). With one worker preload is off: there is nothing to
     share, and the master's copy would only double the footprint.
   - `max_requests` (+ jitter): recycling each worker after a bounded number of
     requests, so any slow memory growth (allocator fragmentation, per-request
     allocations that never return to the OS) is refreshed away long before it
     reaches the instance ceiling and triggers an OOM restart. The jitter
     staggers restarts so workers never recycle in lockstep. With preload a
     recycle is a cheap re-fork from the master, not a re-import.
"""

import os

from infrastructure.log_noise import install_worker_lifecycle_filter


# Importing the app in the master before forking so its heavy module-level
# imports are paid once and shared copy-on-write across every worker. Only
# when there IS more than one worker: with one (WEB_CONCURRENCY unset or 1, the
# Starter shape) nothing is shared, and CPython's reference counting writes to
# nearly every inherited page, so the worker ends up with a private copy of the
# master's heap and the instance holds the app twice. Measured on FLOPX
# (2026-11-08, one worker): master 170 MB PSS + worker 318 MB PSS = 488 MB of a
# 512 MB instance, against which Render's restarts lined up. Reads gunicorn's
# own WEB_CONCURRENCY, so no new setting exists:
preload_app = int(os.environ.get("WEB_CONCURRENCY", "1") or "1") > 1

# Recycling each worker after this many requests (+/- jitter) so gradual memory
# growth is refreshed before it can reach the instance memory limit. Values are
# hardcoded on purpose (config-minimalism: no env var to provision or drift):
# 2026-11-08 (B-PERF): raised from 1000. A page of the FLOPX frontend makes about 30 requests, so
# 1000 recycled the single Starter worker every ~33 page loads; each recycle dropped in-flight
# connections (one worker means no sibling to take them: 6 resets over 36 local recycles) and threw
# away the process's warm snapshot. With the per-read deep copies gone the worker's memory was flat
# over a five-minute soak (305 MB PSS throughout), so a 10x longer life is still a drift backstop.
max_requests = 10000
max_requests_jitter = 1000


def on_starting(server):
    # Running once in the master, before the app is preloaded or any worker forks:
    install_worker_lifecycle_filter()


def post_fork(server, worker):
    # Running in each worker right after fork (the app is already preloaded in
    # the master). Re-installing the log filter for the worker's own logger:
    install_worker_lifecycle_filter()

    # Disposing the SQLAlchemy engine's connection pool the worker inherited from
    # the preloaded master, so each worker opens its OWN database connections
    # rather than sharing (and corrupting) the master's sockets across processes.
    # Imported lazily here so config load never depends on the app importing:
    from infrastructure.database import engine
    engine.dispose()

# Worker heartbeat timeout. The default 30 s is sized for sync workers that only ever run request
# code. FLOPX's single uvicorn worker also runs two background threads (the snapshot refresher and
# the power-price history backfill) on a 0.5-CPU Starter instance; measured 2026-11-08 03:25Z and
# 03:26Z, the worker was aborted (WORKER TIMEOUT, code 134) about 13 s after the backfill took its
# lock while a 14 s recompose was in flight, and every death threw away the warm snapshot and
# answered the frontend with 502s. The heartbeat is a liveness check, not a request deadline
# (requests have their own upstream timeouts), so it is widened to cover a busy minute.
timeout = 120
graceful_timeout = 30
