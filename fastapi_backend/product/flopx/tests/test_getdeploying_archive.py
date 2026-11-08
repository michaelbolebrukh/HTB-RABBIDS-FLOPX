"""GetDeploying's weekly GPU rental price history sits beside FCPI honestly: parsed from a recorded CSV, mapped to the six
FLOPX models and nothing else, stamped at each week's end and held back until then, loaded idempotently at boot and once a
week, never written into an FCPI or on-demand series, and served through the series read with its CC BY 4.0 line.

Every expectation is stated independently of the code: the stored values are the recorded body's own numbers (the CSV at
fixtures/recorded/getdeploying/weekly.json was fetched once from the real host on 2026-11-08 and trimmed to the rows the
adapter must take plus a few it must ignore), the week ends are worked out by hand, and the clock is pinned to
2026-11-08 03:00 UTC so the newest dataset week (beginning Monday 2026-11-02, ending 2026-11-09 00:00 UTC) is still running.
No test opens a network connection.
"""

import json
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import infrastructure.cache as cache_module
import infrastructure.dependencies as deps
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
from infrastructure.rate_limiter import limiter
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.flopx import archive, refresher, series as series_module, terminal
from product.flopx.data.loader import load_sample
from product.flopx.sources import _http, getdeploying, licences
from product.flopx.tools import alpaca_load, compustat_load, getdeploying_load, wrds_load
from product.models import Observation
from product.routers.flopx import router
from product.startup import STARTUP_TASKS, load_research_archive, start_refresher

UTC = timezone.utc
RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "getdeploying" / "weekly.json"
NEXT_PWA_COPY = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)            # Sunday morning: the week of Monday 2026-11-02 is still running
NEXT_MONDAY = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)    # an hour after that week ended
MODELS = ("a100-sxm-80gb", "b200-sxm-192gb", "h100-sxm-80gb", "h200-sxm-141gb", "l40s-48gb", "rtx-4090-24gb")
H100_MEDIAN = "gpu:h100-sxm-80gb:getdeploying:weekly-median"
CC_LINK = "https://creativecommons.org/licenses/by/4.0/"


def _recorded() -> dict:
    return json.loads(RECORDED.read_text(encoding="utf-8"))


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


class Clock:
    """Pins every clock the write and read paths consult (the archive's refusal of a future-dated point, the series read's
    range cutoff, the loader's default), so the running week stays running whatever today is; `set` moves all of them."""

    def __init__(self, monkeypatch):
        self.now = NOW
        for module in (archive, series_module, getdeploying_load):
            monkeypatch.setattr(module, "utcnow", lambda: self.now)

    def set(self, when: datetime) -> None:
        self.now = when


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


class Upstream:
    """A stand-in for getdeploying.com: serves the recorded CSV (or an edited copy), counts requests, can answer a status."""

    def __init__(self):
        rec = _recorded()
        self.body: str = rec["body"]
        self.date = rec["date"]
        self.requests: list[httpx.Request] = []
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "getdeploying.com" or request.url.path != "/dataset/gpu-prices/weekly.csv":
            return httpx.Response(404, text="unrouted in test")
        if self.status is not None:
            return httpx.Response(self.status, headers=self.headers, text=f"status {self.status}")
        return httpx.Response(200, headers={"Date": self.date, "Content-Type": "text/csv; charset=utf-8"}, text=self.body)

    def revise(self, slug: str, week: str, billing: str, column: str, value: str) -> None:
        """Edits one cell of the CSV in place, as an upstream revision would."""
        lines = self.body.splitlines()
        header = lines[0].split(",")
        for i, line in enumerate(lines[1:], start=1):
            cells = line.split(",")
            if cells[0] == slug and cells[1] == week and cells[2] == billing:
                cells[header.index(column)] = value
                lines[i] = ",".join(cells)
        self.body = "\n".join(lines) + "\n"


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(up))
    _http.reset_jobs()
    getdeploying_load.reset_refresh_state()
    try:
        yield up
    finally:
        _http.reset_jobs()
        getdeploying_load.reset_refresh_state()


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))


def _client(user=None) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    app.dependency_overrides[deps.get_db] = lambda: _client.db
    app.dependency_overrides[deps.get_current_user_or_none] = lambda: user
    return TestClient(app)


# -- the licence row ----------------------------------------------------------------------------------

def test_the_licence_row_is_display_and_archive_measured_from_the_dataset_page_and_both_copies_match():
    row = licences.policy("getdeploying")
    assert row["use"] == "display-and-archive" and row["asserted"] is False and row["licence"] == "CC BY 4.0"
    assert row["termsUrl"] == "https://getdeploying.com/dataset/gpu-prices"
    # CC BY 4.0 wants the creator credited, the licence named and linked, and changes indicated; the dataset page asks for
    # "Credit GetDeploying and link to the license". The attribution line carries all of it:
    assert row["attribution"].startswith("GetDeploying (2026). GPU rental price history [dataset]")
    assert "CC BY 4.0" in row["attribution"] and CC_LINK in row["attribution"] and "relabelled with FLOPX model ids" in row["attribution"]
    assert "read 2026-11-08" in row["note"] and "dataset files only" in row["note"] and "never written into index:fcpi:*" in row["note"]
    assert licences.enabled("getdeploying") and licences.archivable("getdeploying") and licences.public("getdeploying")
    assert licences.licence("getdeploying") == "CC BY 4.0"                                   # measured: no asserted caveat
    assert getdeploying.LICENCE == "CC BY 4.0" and getdeploying.ATTRIBUTION == row["attribution"]
    assert "getdeploying" in {r["id"] for r in licences.rows()}                               # listed on the sources page
    assert licences.TABLE_PATH.read_bytes() == NEXT_PWA_COPY.read_bytes()                     # byte-identical copies


# -- parsing and the model mapping ------------------------------------------------------------------------

def test_the_recorded_body_parses_to_the_six_models_weekly_rows_with_the_datasets_own_numbers():
    rec = _recorded()
    assert rec["url"] == "https://getdeploying.com/dataset/gpu-prices/weekly.csv" and rec["status"] == 200
    rows = getdeploying.parse_weekly_csv(rec["body"], NOW)
    assert len(rows) == 318 and {r.gpu_model_id for r in rows} == set(MODELS)
    per_model = {m: [r for r in rows if r.gpu_model_id == m] for m in MODELS}
    assert all(len(per_model[m]) == 53 for m in MODELS)                                      # 53 trailing weeks each
    assert all(r.week_start.weekday() == 0 for r in rows)                                      # every dataset week starts on a Monday
    h100 = per_model["h100-sxm-80gb"]
    first, last = h100[0], h100[-1]
    assert first.gpu_slug == "nvidia-h100" and first.week_start == date(2025, 11, 3)
    assert first.t == datetime(2025, 11, 10, 0, 0, tzinfo=UTC)                                 # stamped at the week's end
    assert (first.median, first.minimum, first.maximum) == (2.98, 0.99, 11.5263)
    assert (first.provider_count, first.offering_count) == (36, 111)
    assert last.week_start == date(2026, 11, 2) and last.t == datetime(2026, 11, 9, 0, 0, tzinfo=UTC) and last.median == 4.0225
    week_43 = next(r for r in h100 if r.week_start == date(2026, 10, 26))
    assert (week_43.median, week_43.minimum, week_43.maximum) == (3.85, 1.7945, 12.29)
    assert [h100[i].t < h100[i + 1].t for i in range(len(h100) - 1)] == [True] * 52           # strictly ascending
    assert {m: per_model[m][0].median for m in MODELS} == {"a100-sxm-80gb": 1.6, "b200-sxm-192gb": 5.39, "h100-sxm-80gb": 2.98,
                                                           "h200-sxm-141gb": 3.5, "l40s-48gb": 1.5244, "rtx-4090-24gb": 0.65}
    prov = first.provenance
    assert prov.kind == "live" and prov.source == "getdeploying" and prov.licence == "CC BY 4.0" and prov.observed_at == first.t
    assert prov.source_url == "https://getdeploying.com/dataset/gpu-prices" and prov.note == getdeploying.ATTRIBUTION
    note = getdeploying.note_for(first)
    assert CC_LINK in note and "Week beginning Monday 2025-11-03" in note and "nvidia-h100 family (36 providers, 111 offerings)" in note
    assert "not an FCPI point" in note


def test_the_mapping_names_six_flopx_models_and_ignores_every_other_slug_and_billing_type():
    assert getdeploying.MODEL_BY_SLUG == {"nvidia-h100": "h100-sxm-80gb", "nvidia-h200": "h200-sxm-141gb", "nvidia-b200": "b200-sxm-192gb",
                                          "nvidia-a100": "a100-sxm-80gb", "nvidia-l40s": "l40s-48gb", "nvidia-rtx-4090": "rtx-4090-24gb"}
    assert set(getdeploying.MODEL_BY_SLUG.values()) <= set(load_sample().gpu_models)          # every target is a real FLOPX model
    body = _recorded()["body"]
    rows = getdeploying.parse_weekly_csv(body, NOW)
    # The recorded body also carries rows the adapter must drop: amd-mi300x and nvidia-rtx-5090 (unmapped), nvidia-h100
    # SPOT (0.9) and RESERVATION (1.705 ...) rows for 2025-11-03. None of them reach a record:
    assert getdeploying.unmapped_slugs(body) == {"amd-mi300x", "nvidia-rtx-5090"}
    assert "amd-mi300x,2025-11-03,ON_DEMAND" in body and not any(r.gpu_slug in ("amd-mi300x", "nvidia-rtx-5090") for r in rows)
    h100_first = [r for r in rows if r.gpu_slug == "nvidia-h100" and r.week_start == date(2025, 11, 3)]
    assert len(h100_first) == 1 and h100_first[0].median == 2.98                               # not the SPOT 0.9 nor a RESERVATION row
    # Unreadable rows are skipped, not raised:
    broken = ("gpu_slug,date,billing_type,reservation_months,min_price,max_price,median_price,provider_median_price,provider_count,offering_count\n"
              "nvidia-h100,not-a-date,ON_DEMAND,,1,2,1.5,1.5,1,1\n"
              "nvidia-h100,2026-01-05,ON_DEMAND,,1,2,,1.5,1,1\n"
              "nvidia-h100,2026-01-12,ON_DEMAND,,1,2,1.5,1.5,1,1\n")
    kept = getdeploying.parse_weekly_csv(broken, NOW)
    assert [(r.week_start, r.median) for r in kept] == [(date(2026, 1, 12), 1.5)]
    assert getdeploying.parse_weekly_csv("", NOW) == []
    assert getdeploying.series_id("h100-sxm-80gb", "weekly-min") == "gpu:h100-sxm-80gb:getdeploying:weekly-min"
    with pytest.raises(ValueError):
        getdeploying.series_id("h100-sxm-80gb", "weekly-mean")
    assert getdeploying.is_series(H100_MEDIAN) and not getdeploying.is_series("gpu:h100-sxm-80gb:on-demand:global")
    assert getdeploying.week_end(date(2026, 11, 2)) == datetime(2026, 11, 9, 0, 0, tzinfo=UTC)


# -- loading --------------------------------------------------------------------------------------------

def test_loading_upserts_three_points_per_ended_week_holds_the_running_week_back_and_never_touches_fcpi(db, upstream, clock, monkeypatch):
    report = getdeploying_load.load(db, now=NOW)
    assert report.unavailable is None and report.requests == 1 and len(upstream.requests) == 1
    assert report.rows == 312 and report.points == 936 and report.series == 18                  # 52 ended weeks x 6 models x 3 measures
    assert (report.inserted, report.updated, report.unchanged) == (936, 0, 0)
    assert report.first_week == date(2025, 11, 3) and report.last_week == date(2026, 10, 26)
    assert report.pending == [date(2026, 11, 2)]                                                 # the running week is held back
    assert report.models == {m: 52 for m in MODELS}
    assert "loaded 312 weeks over 18 series (2025-11-03 to 2026-10-26) as 936 points in 1 request" in report.line()
    assert "pending until the week ends: 2026-11-02" in report.line()
    assert db.query(Observation).count() == 936
    ids = {r.series_id for r in db.query(Observation).all()}
    assert ids == {f"gpu:{m}:getdeploying:{measure}" for m in MODELS for measure in ("weekly-median", "weekly-min", "weekly-max")}
    assert not any(i.startswith("index:fcpi:") or ":on-demand:" in i for i in ids)             # never an FCPI or on-demand point
    first = db.query(Observation).filter(Observation.series_id == H100_MEDIAN).order_by(Observation.t.asc()).first()
    assert first.t.replace(tzinfo=UTC) == datetime(2025, 11, 10, 0, 0, tzinfo=UTC) and first.value == 2.98 and first.unit == "USD/GPU-hour"
    prov = first.provenance
    assert prov["kind"] == "archived" and prov["source"] == "getdeploying" and prov["licence"] == "CC BY 4.0"
    assert prov["source_url"] == "https://getdeploying.com/dataset/gpu-prices" and prov["fetched_at"] == NOW.isoformat()
    assert CC_LINK in prov["note"] and "GetDeploying (2026)" in prov["note"] and "Week beginning Monday 2025-11-03" in prov["note"]
    assert prov["getdeploying"] == {"gpu_slug": "nvidia-h100", "week_start": "2025-11-03", "billing_type": "ON_DEMAND",
                                    "provider_count": 36, "offering_count": 111}                 # the audit trail beside the provenance
    low = db.query(Observation).filter(Observation.series_id == "gpu:h100-sxm-80gb:getdeploying:weekly-min", Observation.t == first.t).one()
    high = db.query(Observation).filter(Observation.series_id == "gpu:h100-sxm-80gb:getdeploying:weekly-max", Observation.t == first.t).one()
    assert (low.value, high.value) == (0.99, 11.5263)
    # A reload writes nothing (and, within the cache window, makes no request):
    again = getdeploying_load.load(db, now=NOW)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 936) and len(upstream.requests) == 1
    # A revised upstream value rewrites exactly that point:
    upstream.revise("nvidia-h100", "2025-11-03", "ON_DEMAND", "median_price", "3.0")
    _fresh_cache(monkeypatch)
    revised = getdeploying_load.load(db, now=NOW)
    assert (revised.inserted, revised.updated, revised.unchanged) == (0, 1, 935) and len(upstream.requests) == 2
    db.expire_all()
    assert db.query(Observation).filter(Observation.series_id == H100_MEDIAN).order_by(Observation.t.asc()).first().value == 3.0
    # Once the running week has ended it lands, three points per model; a dry run beforehand writes nothing:
    clock.set(NEXT_MONDAY)
    dry = getdeploying_load.load(db, now=NEXT_MONDAY, dry_run=True)
    assert dry.dry_run and dry.inserted == 18 and dry.pending == [] and db.query(Observation).count() == 936
    landed = getdeploying_load.load(db, now=NEXT_MONDAY)
    assert (landed.inserted, landed.updated, landed.unchanged) == (18, 0, 936) and landed.pending == []
    assert db.query(Observation).count() == 954
    newest = db.query(Observation).filter(Observation.series_id == H100_MEDIAN).order_by(Observation.t.desc()).first()
    assert newest.t.replace(tzinfo=UTC) == datetime(2026, 11, 9, 0, 0, tzinfo=UTC) and newest.value == 4.0225
    assert getdeploying_load.count_by_model(db) == {m: 53 for m in MODELS}
    assert getdeploying_load.newest_week(db)["h100-sxm-80gb"] == date(2026, 11, 2)


def test_a_failed_fetch_is_a_reason_token_with_no_write_and_a_rate_limit_pauses_the_host(db, upstream, clock, caplog):
    upstream.status = 500
    with caplog.at_level(logging.WARNING):
        result = getdeploying.fetch_weekly()
        report = getdeploying_load.load(db, now=NOW)
    assert not result.ok and result.reason == "http-500" and report.unavailable == "http-500"
    assert report.inserted == 0 and db.query(Observation).count() == 0 and "not loaded (http-500)" in report.line()
    assert len(upstream.requests) == 1                                                            # the failure is cached briefly
    upstream.status, upstream.headers = 429, {"Retry-After": "90"}
    _http.reset_jobs()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        blocked = getdeploying_load.load(db, now=NOW)
    assert blocked.unavailable in ("http-500", "http-429")                                       # the earlier failure may still be cached
    if blocked.unavailable == "http-429":
        assert "host paused 90s" in caplog.text
    assert db.query(Observation).count() == 0


# -- startup and the weekly refresh ---------------------------------------------------------------------

def test_the_startup_seam_refuses_under_pytest_unless_forced_loads_once_and_marks_the_week(db, upstream, clock, caplog, monkeypatch):
    assert STARTUP_TASKS.index(load_research_archive) < STARTUP_TASKS.index(start_refresher)
    order: list[str] = []
    with pytest.MonkeyPatch.context() as stubs:
        stubs.setattr(wrds_load, "load_secret_file_if_present", lambda db, path=None: order.append("wrds-crsp"))
        stubs.setattr(compustat_load, "load_secret_file_if_present", lambda db, path=None: order.append("wrds-compustat"))
        stubs.setattr(alpaca_load, "load_at_startup", lambda db: order.append("alpaca-data"))
        stubs.setattr(getdeploying_load, "load_at_startup", lambda db: order.append("getdeploying"))
        load_research_archive(db)
    assert order == ["wrds-crsp", "wrds-compustat", "alpaca-data", "getdeploying"]
    # Under pytest the real seam makes no request at all (the source is keyless, so this guard is what keeps the suite offline):
    assert getdeploying_load.load_at_startup(db) is None and upstream.requests == [] and db.query(Observation).count() == 0
    assert getdeploying_load.refresh_if_due(lambda: db, now=NOW) is None and upstream.requests == []
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.getdeploying_load"):
        report = getdeploying_load.load_at_startup(db, force=True)
        again = getdeploying_load.load_at_startup(db, force=True)
    assert report.inserted == 936 and again.inserted == 0 and len(upstream.requests) == 1
    assert "getdeploying: loaded 312 weeks over 18 series (2025-11-03 to 2026-10-26) as 936 points in 1 request: 936 inserted" in caplog.text
    assert "held: {'a100-sxm-80gb': 52" in caplog.text
    assert getdeploying_load._refreshed_this_week(getdeploying_load._week_of(NOW)) is True      # the boot load counts as this week's refresh
    assert getdeploying_load.refresh_if_due(lambda: db, now=NOW, force=True) is None
    # A startup failure is one warning line, never a raise:
    upstream.status = 503
    _fresh_cache(monkeypatch)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="product.flopx.tools.getdeploying_load"):
        failed = getdeploying_load.load_at_startup(db, force=True)
    assert failed is not None and failed.unavailable == "http-503" and "not loaded (http-503)" in caplog.text
    assert db.query(Observation).count() == 936


def test_the_weekly_hook_loads_once_an_iso_week_across_calls_and_retries_a_failed_week(db, upstream, clock, monkeypatch):
    first = getdeploying_load.refresh_if_due(lambda: db, now=NOW, force=True)
    assert first is not None and first.inserted == 936 and len(upstream.requests) == 1
    assert getdeploying_load.refresh_if_due(lambda: db, now=NOW, force=True) is None             # done for ISO week 2026-W45
    assert getdeploying_load._week_of(NOW) == "2026-W45" and getdeploying_load._week_of(NEXT_MONDAY) == "2026-W46"
    # The first tick of the next ISO week loads again, and the week that just ended lands:
    _http.reset_jobs()
    _fresh_cache(monkeypatch)
    getdeploying_load._mark_refreshed("2026-W45")
    clock.set(NEXT_MONDAY)
    second = getdeploying_load.refresh_if_due(lambda: db, now=NEXT_MONDAY, force=True)
    assert second is not None and second.inserted == 18 and second.pending == [] and len(upstream.requests) == 2
    assert getdeploying_load.refresh_if_due(lambda: db, now=NEXT_MONDAY, force=True) is None
    # A failed week is not marked done; it is retried after the retry clock, not every tick:
    _fresh_cache(monkeypatch)
    getdeploying_load.reset_refresh_state()
    upstream.status = 500
    later = datetime(2026, 11, 16, 0, 30, tzinfo=UTC)
    clock.set(later)
    failed = getdeploying_load.refresh_if_due(lambda: db, now=later, force=True)
    assert failed is not None and failed.unavailable == "http-500" and len(upstream.requests) == 3
    assert getdeploying_load._refreshed_this_week("2026-W47") is False
    assert getdeploying_load.refresh_if_due(lambda: db, now=later, force=True) is None and len(upstream.requests) == 3   # inside the retry clock
    assert getdeploying_load.refresh_if_due(None, now=later, force=True) is None                 # no session factory: nothing to do
    assert db.query(Observation).count() == 954


def test_the_refresher_loop_calls_the_weekly_hook_with_its_session_factory(monkeypatch):
    calls: list[object] = []
    stop = threading.Event()

    def hook(session_factory):
        calls.append(session_factory)
        stop.set()
        return None

    monkeypatch.setattr(getdeploying_load, "refresh_if_due", hook)
    monkeypatch.setattr(refresher, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(refresher, "recompose_due", lambda session_factory: 0)
    monkeypatch.setattr(_http, "refresh_due", lambda pool=None: 0)
    factory = object()
    thread = threading.Thread(target=refresher._run, args=(stop, factory, lambda: None, False), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and calls == [factory]


# -- who sees the points: the series read ---------------------------------------------------------------

def test_the_series_read_serves_the_weekly_series_archived_with_the_licence_line_and_leaves_fcpi_alone(db, upstream, clock):
    getdeploying_load.load(db, now=NOW)
    for measure, first_value in (("weekly-median", 2.98), ("weekly-min", 0.99), ("weekly-max", 11.5263)):
        view = terminal.series(db, f"gpu:h100-sxm-80gb:getdeploying:{measure}", "10y", None)
        points = view["points"]
        assert view["series_id"] == f"gpu:h100-sxm-80gb:getdeploying:{measure}" and view["unit"] == "USD/GPU-hour"
        assert len(points) == 52 and points[0].t == datetime(2025, 11, 10, 0, 0, tzinfo=UTC) and points[0].value == first_value
        assert points[-1].t == datetime(2026, 11, 2, 0, 0, tzinfo=UTC)                           # the running week is not served
        assert {p.provenance.kind for p in points} == {"archived"} and {p.provenance.source for p in points} == {"getdeploying"}
        assert all(CC_LINK in p.provenance.note for p in points)
        fresh = view["freshness"]
        assert fresh.sources == ("getdeploying",) or list(fresh.sources) == ["getdeploying"]
        assert [d["licence"] for d in fresh.source_details] == ["CC BY 4.0"]
    # An anonymous reader gets them too (the row is display-and-archive, not research-archive):
    _client.db = db
    anon = _client(None).get("/gpu/series", params={"id": H100_MEDIAN, "range": "10y"})
    body = anon.json()
    assert anon.status_code == 200 and len(body["points"]) == 52 and body["freshness"]["sources"] == ["getdeploying"]
    assert body["points"][0]["t"] == "2025-11-10T00:00:00Z" and body["points"][0]["value"] == 2.98
    assert body["points"][0]["provenance"]["kind"] == "archived" and body["points"][0]["provenance"]["licence"] == "CC BY 4.0"
    assert body["freshness"]["sourceDetails"][0]["licence"] == "CC BY 4.0"
    # The on-demand sample series and the FCPI index carry no GetDeploying point:
    sample = terminal.series(db, "gpu:h100-sxm-80gb:on-demand:global", "10y", None)
    assert "getdeploying" not in sample["freshness"].sources and {p.provenance.kind for p in sample["points"]} == {"sample"}
    assert archive.archived_series(db, "index:fcpi:h100-sxm-80gb") is None
    assert archive.archived_series(db, "gpu:h100-sxm-80gb:on-demand:global") is None


def test_the_pressure_inputs_read_the_weekly_median_and_momentum_falls_back_to_it_on_every_sample_cell(db, upstream, clock, monkeypatch):
    """N-GDH-BE over the recorded weeks: the sample asks carry no FLOPX index, so with the archive loaded every cell of
    every model reads GetDeploying's weekly median, labelled. Hand numbers from the recorded CSV: the sample as_of is
    Friday 2026-11-06 21:00, the last week ending by then is 2026-11-02 (the week beginning 2026-10-26: h100 3.85), the
    window reaches back to 2026-10-03 and the latest week end at or before that is 2026-09-28 (the week beginning
    2026-09-21: h100 3.225), so h100 raw = 3.85 / 3.225 - 1 = 0.1938 and normalised clamps to 1.0."""
    from product.flopx import pressure

    getdeploying_load.load(db, now=NOW)
    monkeypatch.setattr(terminal, "_gather", lambda calls: [])          # no live source answers; the recorded archive is all there is
    inputs = terminal._inputs(db, load_sample())
    weekly = inputs.series[H100_MEDIAN]
    assert len(weekly.points) == 52 and weekly.points[-1].t == datetime(2026, 11, 2, 0, 0, tzinfo=UTC) and weekly.points[-1].value == 3.85
    assert inputs.series["gpu:h100-sxm-80gb:on-demand:global"].points[-1].provenance.kind == "sample"     # the sample series is not touched
    assert "index:fcpi:h100-sxm-80gb" not in inputs.series                                                  # no live asks: no FLOPX index
    expected = {"h100-sxm-80gb": (3.85, 3.225), "a100-sxm-80gb": (1.79, 1.6), "b200-sxm-192gb": (7.83, 6.5318),
                "h200-sxm-141gb": (4.81, 4.4), "l40s-48gb": (1.795, 1.5039), "rtx-4090-24gb": (0.6, 0.4837)}
    cells = pressure.compute_grid(offers=inputs.offers, as_of=inputs.as_of, **inputs.cell_kwargs)
    assert len(cells) == 8 * 6
    for c in cells:
        f = {f.factor: f for f in c.factors}["price-momentum"]
        last, earlier = expected[c.gpu_model_id]
        assert f.scope == "getdeploying-weekly" and f.raw == pytest.approx(last / earlier - 1, abs=1e-4), (c.region_id, c.gpu_model_id)
        assert f.note.startswith("30-day change of GetDeploying's weekly median (CC BY 4.0), FLOPX index history too short (0 days); ")
        assert f"gpu:{c.gpu_model_id}:getdeploying:weekly-median: {last:.4f} (week ending 2026-11-02) vs {earlier:.4f} (week ending 2026-09-28), 35 days apart" in f.note
        assert {p.kind for p in f.evidence} == {"archived"} and {p.source for p in f.evidence} == {"getdeploying"} and {p.licence for p in f.evidence} == {"CC BY 4.0"}
        assert "archived" in c.input_kinds and c.uncertainty.sample_share < 1.0
    h100_tx = next(c for c in cells if c.region_id == "us-tx" and c.gpu_model_id == "h100-sxm-80gb")
    assert {f.factor: f for f in h100_tx.factors}["price-momentum"].normalised == 1.0
    # Without a database nothing is read, so the sample path the engine fixtures pin is unchanged:
    bare = terminal._inputs(None, load_sample())
    assert H100_MEDIAN not in bare.series
    f = {f.factor: f for f in pressure.compute_cell("us-tx", "h100-sxm-80gb", offers=bare.offers, as_of=bare.as_of, **bare.cell_kwargs).factors}["price-momentum"]
    assert f.scope == "global" and f.note.startswith("gpu:h100-sxm-80gb:on-demand:global:")


# -- removal and the CLI ---------------------------------------------------------------------------------

def test_delete_removes_only_getdeploying_rows_and_the_cli_loads_reports_and_deletes(db, upstream, clock, monkeypatch, capsys):
    import infrastructure.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    other = Observation(series_id="gpu:h100-sxm-80gb:on-demand:global", t=datetime(2026, 1, 5, tzinfo=UTC), value=2.5, unit="USD/GPU-hour",
                        provenance={"kind": "live", "source": "runpod", "observed_at": "2026-01-05T00:00:00+00:00"})
    db.add(other)
    db.commit()
    assert getdeploying_load.main(["load", "--dry-run"]) == 0 and db.query(Observation).count() == 1
    assert getdeploying_load.main(["load"]) == 0 and db.query(Observation).count() == 937
    assert getdeploying_load.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "936 inserted, 0 updated, 0 unchanged" in out and "h100-sxm-80gb: 52 weeks" in out
    assert "312 weeks held" in out and "rtx-4090-24gb: 52 (newest week beginning 2026-10-26)" in out
    assert getdeploying_load.delete_all(db, dry_run=True) == 936 and db.query(Observation).count() == 937
    assert getdeploying_load.main(["delete", "--dry-run"]) == 0 and db.query(Observation).count() == 937
    assert getdeploying_load.main(["delete"]) == 0
    assert [r.series_id for r in db.query(Observation).all()] == ["gpu:h100-sxm-80gb:on-demand:global"]   # the RunPod row survives
    assert getdeploying_load.delete_all(db) == 0 and getdeploying_load.count_by_model(db) == {}
    upstream.status = 500
    _fresh_cache(monkeypatch)
    assert getdeploying_load.main(["load"]) == 1
