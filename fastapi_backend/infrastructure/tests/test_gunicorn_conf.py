"""Tests for the gunicorn config's memory defaults (gunicorn.conf.py).

The config file lives at the backend root and is normally auto-loaded by
gunicorn from the CWD. Its module name (`gunicorn.conf`) would clash with the
installed `gunicorn` package, so it is loaded here by file path under a private
name. These tests pin the two memory behaviours that ship on by default —
preload and worker recycling — plus the fork-time hooks, so a future edit that
silently drops one fails CI.
"""

import importlib.util
import logging
import os

import pytest

from infrastructure.log_noise import WorkerLifecycleNoiseFilter


BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONF_PATH = os.path.join(BACKEND_ROOT, "gunicorn.conf.py")


def _load_conf():
    """Loading gunicorn.conf.py by path under a non-clashing module name:"""
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", CONF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def conf():
    return _load_conf()


def test_preload_app_is_on_only_when_there_are_workers_to_share_with(monkeypatch):
    # Shared copy-on-write imports across workers require preload; with a single worker
    # (WEB_CONCURRENCY unset or 1) nothing is shared and the master's copy would double the
    # footprint (measured 2026-11-08: 170 MB master + 318 MB worker PSS on a 512 MB instance).
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert _load_conf().preload_app is False
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert _load_conf().preload_app is False
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    assert _load_conf().preload_app is True


def test_max_requests_recycling_is_configured(conf):
    # Bounded worker lifetime is the drift backstop; both values must be sane:
    assert isinstance(conf.max_requests, int) and conf.max_requests > 0
    assert isinstance(conf.max_requests_jitter, int) and conf.max_requests_jitter >= 0
    # Jitter must be smaller than the base so the recycle window stays positive:
    assert conf.max_requests_jitter < conf.max_requests


def test_on_starting_installs_worker_lifecycle_filter(conf):
    logger = logging.getLogger("gunicorn.error")
    # Snapshotting so this test leaves the shared logger exactly as it found it:
    original = list(logger.filters)
    try:
        logger.filters = [f for f in logger.filters if not isinstance(f, WorkerLifecycleNoiseFilter)]
        conf.on_starting(server=None)
        assert any(isinstance(f, WorkerLifecycleNoiseFilter) for f in logger.filters)
    finally:
        logger.filters = original


def test_post_fork_installs_filter_and_disposes_engine_pool(conf, monkeypatch):
    # post_fork must dispose the DB pool inherited from the preloaded master so
    # each worker opens its own connections (never sharing sockets across forks):
    from infrastructure import database

    calls = {"dispose": 0}
    monkeypatch.setattr(database.engine, "dispose", lambda: calls.__setitem__("dispose", calls["dispose"] + 1))

    logger = logging.getLogger("gunicorn.error")
    original = list(logger.filters)
    try:
        logger.filters = [f for f in logger.filters if not isinstance(f, WorkerLifecycleNoiseFilter)]
        conf.post_fork(server=None, worker=None)
        assert calls["dispose"] == 1
        assert any(isinstance(f, WorkerLifecycleNoiseFilter) for f in logger.filters)
    finally:
        logger.filters = original
