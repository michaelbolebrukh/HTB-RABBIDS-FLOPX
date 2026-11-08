"""Taming gunicorn worker-lifecycle log noise.

A graceful worker shutdown (SIGTERM — e.g. on every deploy) is logged by the
gunicorn arbiter at ERROR level ("Worker (pid:X) was sent SIGTERM!"), which
turns every deploy into a tracked error/alert. This downgrades ONLY that
benign message to INFO: it stays visible as a deploy marker in the logs but is
no longer an error, so error tracking (which captures ERROR and above) skips
it. Genuinely actionable worker deaths are left untouched at their original
level — SIGKILL/OOM ("Perhaps out of memory?"), WORKER TIMEOUT, and boot
failures still surface as errors.

Installed on the `gunicorn.error` logger in both the master and each worker
(see gunicorn.conf.py), so the downgrade happens where the record is emitted,
independent of the error-tracking delivery path. No settings import — safe to
load from the gunicorn config in the master before the app is imported.
"""

import logging


class WorkerLifecycleNoiseFilter(logging.Filter):
    """Downgrades the benign graceful-shutdown SIGTERM message to INFO."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        # A worker being *asked* to shut down (SIGTERM) is normal on every
        # deploy/restart — keep it as an INFO marker, never an error. SIGKILL
        # (OOM), timeouts, and boot failures carry different text and are left
        # at ERROR/CRITICAL so they still alert:
        if "was sent SIGTERM" in message:
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return True


def install_worker_lifecycle_filter() -> None:
    """Attaches the filter to gunicorn's error logger (idempotent)."""
    logger = logging.getLogger("gunicorn.error")
    if not any(isinstance(f, WorkerLifecycleNoiseFilter) for f in logger.filters):
        logger.addFilter(WorkerLifecycleNoiseFilter())
