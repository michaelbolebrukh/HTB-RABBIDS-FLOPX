"""B-PERF: reads answer from a warm snapshot and never wait on an upstream.

Pinned here, against the recorded upstream responses (the same `httpx.MockTransport` seam and
realistic shapes as test_flopx_sources_live: RunPod and Akash asks carry no region, Vast asks do):

- in serving mode a read returns at once while a 10 s upstream is still in flight, falls back to the
  sample tier labelled `warming` before the first answer, and serves live rows once it lands;
- an answer keeps the time its source gave it (observedAt) however late it is served, and an answer
  older than the stale ceiling is not served at all;
- a failed refresh keeps the last answer and backs off by the host's Retry-After;
- the refresher seeds only licensed sources, refreshes due jobs, and recomposes snapshots after an
  upstream change; a served composition keeps its own generatedAt;
- the shared sample object is one object per process and no composed read mutates it.
No test opens a network connection.
"""
import pickle
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from product.flopx import refresher, terminal
from product.flopx.data.loader import load_sample
from product.flopx.sources import _http, licences, runpod
from test_flopx_sources_live import recorded_handler

RUNPOD_DATE = datetime(2026, 11, 7, 15, 51, 9, tzinfo=timezone.utc)   # the recorded response's Date header


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    _http.reset_jobs()
    refresher.reset()
    yield
    refresher.stop()
    _http.serve_from_snapshots(False)
    _http.reset_jobs()
    refresher.reset()


class SlowRunpod:
    """The recorded upstreams, except that RunPod takes up to 10 s to answer (until released)."""

    def __init__(self):
        self.release = threading.Event()
        self.runpod_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.runpod.io":
            self.runpod_calls += 1
            self.release.wait(10)
        return recorded_handler(request)


def _offers():
    return terminal.offers(None, None, None, None, None, None)


def test_a_read_never_waits_on_a_ten_second_upstream_and_labels_the_sample_fallback(monkeypatch):
    slow = SlowRunpod()
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(slow))
    _http.serve_from_snapshots(True)

    started = time.perf_counter()
    cold = _offers()
    assert time.perf_counter() - started < 1.0
    assert slow.runpod_calls == 0                                     # a request thread asked nobody
    reasons = {(u["source"], u["reason"]) for u in cold["freshness"].unavailable}
    assert ("runpod", "warming") in reasons and ("akash", "warming") in reasons
    assert all(o["provenance"].kind == "sample" for o in cold["offers"])

    # The refresher fetches; RunPod hangs for up to 10 s, and a read meanwhile still answers at once.
    refreshing = threading.Thread(target=_http.refresh_due)
    refreshing.start()
    for _ in range(100):
        if slow.runpod_calls:
            break
        time.sleep(0.01)
    assert slow.runpod_calls == 1
    started = time.perf_counter()
    during = _offers()
    assert time.perf_counter() - started < 1.0
    assert ("runpod", "warming") in {(u["source"], u["reason"]) for u in during["freshness"].unavailable}

    slow.release.set()
    refreshing.join(5)
    after = _offers()
    live = [o for o in after["offers"] if o["provenance"].source == "runpod"]
    assert live and all(o["region_id"] == "global" for o in live)     # RunPod publishes no region
    # observedAt is when RunPod answered (its Date header), not when the refresher or the read ran:
    assert {o["provenance"].observed_at for o in live} == {RUNPOD_DATE}
    assert not any(u["source"] == "runpod" for u in after["freshness"].unavailable)


def test_a_stale_answer_is_served_with_its_own_time_and_marked_due(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    first = runpod.fetch_gpu_offers()                                 # not serving: fetched through the cache
    assert first.ok
    _http.serve_from_snapshots(True)
    job = _http._jobs["flopx:runpod:gpu-types"]
    job.answered_at -= runpod.CACHE_TTL_SECONDS + 1                    # older than its TTL
    job.due_at = time.monotonic() + 999
    served = runpod.fetch_gpu_offers()
    assert served.ok and served.provenance.observed_at == RUNPOD_DATE
    assert job.due_at <= time.monotonic()                              # the refresher will fetch it next tick


def test_an_answer_past_the_stale_ceiling_is_not_served(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    assert runpod.fetch_gpu_offers().ok
    _http.serve_from_snapshots(True)
    _http._jobs["flopx:runpod:gpu-types"].answered_at -= runpod.CACHE_TTL_SECONDS * _http.STALE_CEILING + 1
    served = runpod.fetch_gpu_offers()
    assert not served.ok and served.reason == "warming"


def test_a_rate_limited_refresh_keeps_the_last_answer_and_waits_for_retry_after(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    assert runpod.fetch_gpu_offers().ok
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"})))
    _http.serve_from_snapshots(True)
    job = _http._jobs["flopx:runpod:gpu-types"]
    job.due_at = 0
    before = time.monotonic()
    assert _http.refresh_due() == 0
    assert job.last_failure.reason == "http-429"
    assert job.due_at >= before + 120                                  # not retried before the host said so
    served = runpod.fetch_gpu_offers()
    assert served.ok and served.provenance.observed_at == RUNPOD_DATE   # the last answer, truthfully dated


def test_the_refresher_seeds_only_licensed_sources_and_recomposes_after_an_answer(monkeypatch):
    slow = SlowRunpod()
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(slow))
    real_enabled = licences.enabled
    monkeypatch.setattr(licences, "enabled", lambda source: source != "vast-ai" and real_enabled(source))
    monkeypatch.setattr(refresher, "RECOMPOSE_MIN_GAP_SECONDS", 0)
    monkeypatch.setattr(refresher, "TICK_SECONDS", 0.05)
    assert refresher.start() is False                                  # never under pytest unless forced
    assert refresher.start(force=True, session_factory=lambda: None, warm=False) is True
    assert refresher.start(force=True) is False                       # one per process
    try:
        for _ in range(200):                                           # the seeded RunPod fetch is in flight
            if slow.runpod_calls:
                break
            time.sleep(0.01)
        cold = _offers()                                               # first read composes inline, from what is held
        assert ("runpod", "warming") in {(u["source"], u["reason"]) for u in cold["freshness"].unavailable}
        slow.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = _offers()
            if any(o["provenance"].source == "runpod" for o in current["offers"]):
                break
            time.sleep(0.05)
        else:
            pytest.fail("the snapshot was never recomposed with the live answer")
    finally:
        refresher.stop()
    assert not any(k.startswith("flopx:vast") for k in _http._jobs)   # an `off` source is never called
    assert "flopx:runpod:gpu-types" in _http.seed_keys() and "flopx:akash:gpu-prices" in _http.seed_keys()
    assert cold["freshness"].generated_at < current["freshness"].generated_at
    assert not _http.serving()                                         # a stopped refresher hands reads back to fetching


def test_a_served_composition_keeps_its_generated_at_and_is_shared_without_a_deep_copy(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    monkeypatch.setattr(refresher, "running", lambda: True)
    _http.serve_from_snapshots(True)
    first = terminal.pressure_grid(None, None, None)
    time.sleep(0.02)
    again = terminal.pressure_grid(None, None, None)
    assert again["freshness"].generated_at == first["freshness"].generated_at   # when composed, not when served
    assert again["cells"] is first["cells"]                                       # one shared composition
    again["cells"] = []                                                           # a reader's top-level change stays its own
    assert terminal.pressure_grid(None, None, None)["cells"] is first["cells"]
    # Recomposition after an upstream change produces a newer generatedAt:
    refresher.note_upstream_change()
    assert refresher.recompose_due(None) >= 1
    assert terminal.pressure_grid(None, None, None)["freshness"].generated_at > first["freshness"].generated_at


def test_the_overview_snapshot_holds_no_reader_portfolio(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    shared = terminal._overview_shared(None)
    assert "portfolio_summary" not in shared
    assert terminal.overview(None, None)["portfolio_summary"] is None


def test_the_shared_sample_is_one_object_and_no_read_mutates_it(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    data = load_sample()
    assert load_sample() is data
    fingerprint = pickle.dumps(data)
    for read in (lambda: terminal.overview(None, None), _offers, lambda: terminal.heatmap(None, "median-adjusted-price", "region"),
                 lambda: terminal.indices(None), lambda: terminal.changes(None, None), lambda: terminal.regions(),
                 lambda: terminal.companies(None), lambda: terminal.markets(None), lambda: terminal.feed(None, None, None, None, None, None),
                 lambda: terminal.pressure_grid(None, None, None), lambda: terminal.effective_supply_view(None, None),
                 lambda: terminal.anomalies_view(None, None, None, None), lambda: terminal.infra_energy(None),
                 lambda: terminal.infra_grid(None), lambda: terminal.infra_projects(None, None), lambda: terminal.evaluation_portfolio(None),
                 lambda: terminal.exposure_view(None, "us-tx", None), lambda: terminal.facilities(None, None)):
        read()
    assert load_sample() is data and pickle.dumps(data) == fingerprint


def test_a_recompose_sweep_builds_the_inferred_inputs_once(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    monkeypatch.setattr(refresher, "running", lambda: True)
    _http.serve_from_snapshots(True)
    for read in (lambda: terminal.pressure_grid(None, None, None), lambda: terminal.infra_grid(None), lambda: terminal.infra_energy(None)):
        read()
    built = []
    real_init = terminal.InferredInputs.__init__

    def counting_init(self, *args, **kwargs):
        built.append(kwargs.get("with_news", True))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(terminal.InferredInputs, "__init__", counting_init)
    refresher.note_upstream_change()
    assert refresher.recompose_due(None) == 3
    assert sorted(built) == [False, True]          # one build per news flag for the whole sweep, not one per read
    built.clear()
    terminal.pressure_grid.uncomposed(None, None, None)   # outside a sweep every composition builds its own
    assert built == [True]


def test_the_evaluation_snapshot_is_recomposed_on_age_not_on_every_upstream_answer(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(recorded_handler))
    monkeypatch.setattr(refresher, "running", lambda: True)
    _http.serve_from_snapshots(True)
    first = terminal.evaluation_portfolio(None)
    terminal.pressure_grid(None, None, None)
    refresher.note_upstream_change()
    assert refresher.recompose_due(None) == 1                       # the pressure grid only
    assert terminal.evaluation_portfolio(None)["freshness"].generated_at == first["freshness"].generated_at
    key = next(k for k in refresher._snapshots if "evaluation_portfolio" in k)
    assert refresher._snapshots[key].ttl == terminal.EVALUATION_CACHE_TTL_SECONDS
    refresher._snapshots[key].composed_at -= terminal.EVALUATION_CACHE_TTL_SECONDS
    assert refresher.recompose_due(None) == 1                       # now its age is due
    assert terminal.evaluation_portfolio(None)["freshness"].generated_at > first["freshness"].generated_at
