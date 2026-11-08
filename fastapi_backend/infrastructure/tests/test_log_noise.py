import logging

from infrastructure.log_noise import (
    WorkerLifecycleNoiseFilter,
    install_worker_lifecycle_filter,
)


def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("gunicorn.error", level, __file__, 1, msg, None, None)


def test_graceful_sigterm_is_downgraded_to_info():
    f = WorkerLifecycleNoiseFilter()
    rec = _record("Worker (pid:67) was sent SIGTERM!", logging.ERROR)
    assert f.filter(rec) is True  # kept, not dropped
    assert rec.levelno == logging.INFO
    assert rec.levelname == "INFO"


def test_sigkill_oom_stays_error():
    f = WorkerLifecycleNoiseFilter()
    rec = _record("Worker (pid:67) was sent SIGKILL! Perhaps out of memory?", logging.ERROR)
    f.filter(rec)
    assert rec.levelno == logging.ERROR  # OOM must still alert


def test_worker_timeout_stays_critical():
    f = WorkerLifecycleNoiseFilter()
    rec = _record("WORKER TIMEOUT (pid:67)", logging.CRITICAL)
    f.filter(rec)
    assert rec.levelno == logging.CRITICAL  # a hung worker must still alert


def test_boot_failure_stays_error():
    f = WorkerLifecycleNoiseFilter()
    rec = _record("Worker failed to boot.", logging.ERROR)
    f.filter(rec)
    assert rec.levelno == logging.ERROR


def test_install_is_idempotent():
    logger = logging.getLogger("gunicorn.error")
    # Starting from a known-clean logger rather than from whatever the process
    # happens to have installed: `gunicorn.error` is a process-global, and any
    # test that imports `main` (which calls configure_logging) installs this
    # filter as a side effect. Reading "before" off a polluted logger made this
    # test fail purely on collection ORDER, which says nothing about the code:
    original = list(logger.filters)
    for existing in original:
        logger.removeFilter(existing)
    try:
        install_worker_lifecycle_filter()
        install_worker_lifecycle_filter()
        added = [f for f in logger.filters if isinstance(f, WorkerLifecycleNoiseFilter)]
        assert len(added) == 1
    finally:
        for f in list(logger.filters):
            logger.removeFilter(f)
        for existing in original:
            logger.addFilter(existing)
