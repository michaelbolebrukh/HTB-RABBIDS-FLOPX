"""The 2026 closes from Alpaca Market Data continue the CRSP series honestly: parsed from recorded responses, split-adjusted
on both sides of the join, stamped at the session close, loaded idempotently at boot and once a day, and served to the
readers the licence row allows.

Every expectation is stated independently of the code. The recorded responses under fixtures/recorded/alpaca/ were
fetched once from the real host on 2026-11-08 (one request each) and are served here through `httpx.MockTransport`; no
test opens a network connection, and the keys the tests send are dummies.
"""

import json
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
from product.flopx import archive, refresher, terminal
from product.flopx.sources import _http, alpaca_data, licences, sec, unavailable, yahoo
from product.flopx.tools import alpaca_load, compustat_load, wrds_load
from product.models import Observation
from product.routers.flopx import router
from product.startup import STARTUP_TASKS, load_research_archive, start_refresher

UTC = timezone.utc
RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "alpaca"
NEXT_PWA_COPY = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
KEY, SECRET = "test-key-id", "test-secret-key"
# A fictional CRSP tail for NVDA: the last three sessions of 2025 (factor 1, so adjusted == raw) and one pre-split day in
# June 2024 whose raw close of 1210.90 under a cumulative factor of 10 adjusts to 121.09, the figure Alpaca's
# split-adjusted bar for that day carries in the recorded response.
CRSP_TAIL = "date,ticker,permno,close,ret,volume,shares_out,cfacpr\n" + "\n".join([
    "2024-06-06,NVDA,86580,1210.900000,0.0,1000,1000,10.000000000000",
    "2025-12-29,NVDA,86580,190.100000,0.0,1000,1000,1.000000000000",
    "2025-12-30,NVDA,86580,188.400000,0.0,1000,1000,1.000000000000",
    "2025-12-31,NVDA,86580,186.500000,0.0,1000,1000,1.000000000000",
]) + "\n"


def _recorded(name: str) -> dict:
    return json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))


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


class Upstream:
    """A stand-in for data.alpaca.markets: answers from the recorded bodies by symbol and start day, counts requests,
    insists on the key headers, and can be told to answer a status instead."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.bodies: dict[tuple[str, str], dict] = {
            ("NVDA", "2026-01-01"): _recorded("bars-nvda-2026-01")["body"],
            ("NVDA", "2024-06-06"): _recorded("bars-nvda-smci-2024-splits")["body"],
            ("SMCI", "2024-06-06"): _recorded("bars-nvda-smci-2024-splits")["body"],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "data.alpaca.markets" or request.url.path != "/v2/stocks/bars":
            return httpx.Response(404, text="unrouted in test")
        if request.headers.get("APCA-API-KEY-ID") != KEY or request.headers.get("APCA-API-SECRET-KEY") != SECRET:
            return httpx.Response(401, json={"message": "unauthorized"})
        if self.status is not None:
            return httpx.Response(self.status, headers=self.headers, json={"message": f"status {self.status}"})
        q = dict(request.url.params)
        body = self.bodies.get((q["symbols"], q["start"]), {"bars": {}, "next_page_token": None})
        return httpx.Response(200, headers={"Date": "Sun, 08 Nov 2026 02:55:00 GMT", "Content-Type": "application/json"}, json=body)

    def asked(self, symbol: str) -> list[dict]:
        return [dict(r.url.params) for r in self.requests if dict(r.url.params).get("symbols") == symbol]


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(up))
    monkeypatch.setattr(alpaca_data.s, "PRODUCT_ALPACA_API_KEY", KEY)
    monkeypatch.setattr(alpaca_data.s, "PRODUCT_ALPACA_SECRET_KEY", SECRET)
    _http.reset_jobs()
    alpaca_load.reset_refresh_state()
    try:
        yield up
    finally:
        _http.reset_jobs()
        alpaca_load.reset_refresh_state()


@pytest.fixture
def crsp_tail(tmp_path) -> Path:
    path = tmp_path / "crsp.csv"
    path.write_text(CRSP_TAIL, encoding="utf-8")
    return path


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=7, email="member@test.local")


def _yahoo_rate_limited(monkeypatch):
    """Yahoo answers 429 (as it does on Render, permanently) and SEC is silent, so the company read falls through."""
    monkeypatch.setattr(yahoo, "fetch_bars", lambda ticker, range_="6mo", interval="1d": unavailable("yahoo-finance", "http-429"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_frame", lambda: unavailable("sec-edgar", "timeout"))


# -- the licence row ----------------------------------------------------------------------------------

def test_the_alpaca_row_is_research_archive_because_the_terms_allow_personal_use_only_and_both_copies_match():
    row = licences.policy("alpaca-data")
    assert row["use"] == "research-archive" and row["asserted"] is False
    assert row["termsUrl"] == "https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf"
    assert "personal and noncommercial" in row["licence"] and "signed-in accounts only" in row["licence"]
    assert "2026-11-08" in row["note"] and "Not display-and-archive" in row["note"]
    assert licences.archivable("alpaca-data") is True and licences.public("alpaca-data") is False
    assert licences.enabled("alpaca-data") is True and licences.in_index("alpaca-data") is True
    assert licences.licence("alpaca-data") == row["licence"]                      # measured, not asserted: no caveat suffix
    assert "alpaca-data" in {r["id"] for r in licences.rows()}
    assert alpaca_data.LICENCE == row["licence"]
    if not NEXT_PWA_COPY.exists():
        pytest.skip("the next-pwa tree is not in this checkout")
    assert NEXT_PWA_COPY.read_bytes() == licences.TABLE_PATH.read_bytes()


# -- parsing, adjustment and observation time ----------------------------------------------------------

def test_a_recorded_response_parses_to_split_adjusted_closes_stamped_at_the_session_close():
    fetched_at = datetime(2026, 11, 8, 3, tzinfo=UTC)
    bars = alpaca_data.parse_bars("NVDA", _recorded("bars-nvda-2026-01")["body"], fetched_at)
    # Six sessions, 2 to 9 January 2026 (the 1st is a holiday, the 3rd and 4th a weekend), closes as Alpaca sent them:
    assert [(alpaca_data.session_day(b.t).isoformat(), b.close) for b in bars] == [
        ("2026-01-02", 188.825), ("2026-01-05", 188.13), ("2026-01-06", 187.255), ("2026-01-07", 189.17), ("2026-01-08", 185.09), ("2026-01-09", 184.82)]
    # Alpaca keys the bar at midnight New York (05:00 UTC in January); the close was known at 16:00 New York, 21:00 UTC:
    assert bars[0].t == datetime(2026, 1, 2, 21, 0, tzinfo=UTC) and bars[0].t.hour != 5
    assert bars[0].provenance.kind == "live" and bars[0].provenance.source == "alpaca-data"
    assert bars[0].provenance.licence == licences.licence("alpaca-data") and bars[0].provenance.fetched_at == fetched_at
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].volume) == (189.8, 192.885, 188.27, 2742114)
    # The June 2024 bars, asked with adjustment=split, already read in post-split terms on the day before NVDA's 10-for-1
    # (121.09, not 1,210.9) and months before SMCI's October 10-for-1 (77.79, not 777.9), and a summer close is 20:00 UTC:
    splits = _recorded("bars-nvda-smci-2024-splits")["body"]
    nvda = alpaca_data.parse_bars("NVDA", splits, fetched_at)
    smci = alpaca_data.parse_bars("SMCI", splits, fetched_at)
    assert [(alpaca_data.session_day(b.t).isoformat(), b.close) for b in nvda][:2] == [("2024-06-06", 121.09), ("2024-06-07", 120.84)]
    assert [(alpaca_data.session_day(b.t).isoformat(), b.close) for b in smci][:2] == [("2024-06-06", 77.79), ("2024-06-07", 76.9)]
    assert nvda[0].t == datetime(2024, 6, 6, 20, 0, tzinfo=UTC)
    assert alpaca_data.session_close_utc(date(2026, 1, 2)) == wrds_load.session_close_utc(date(2026, 1, 2))   # one rule, both loaders
    # A bar with no close or no time is skipped, an unknown symbol gives nothing, and a ticker's bars sort oldest first:
    ragged = {"bars": {"X": [{"t": "2026-02-03T05:00:00Z", "c": 2.0}, {"t": "2026-02-02T05:00:00Z", "c": 1.0}, {"t": "2026-02-04T05:00:00Z"}, {"c": 3.0}]}}
    assert [b.close for b in alpaca_data.parse_bars("X", ragged, fetched_at)] == [1.0, 2.0]
    assert alpaca_data.parse_bars("Y", ragged, fetched_at) == []
    obs, extra = alpaca_load.observations(bars, loaded_at=fetched_at)
    assert [o.series_id for o in obs] == ["equity:NVDA:close"] * 6 and obs[0].unit == "USD"
    assert obs[0].provenance.kind == "archived" and obs[0].provenance.source == "alpaca-data" and obs[0].t == bars[0].t
    assert extra[id(obs[0])] == {"alpaca": {"open": 189.8, "high": 192.885, "low": 188.27, "volume": 2742114, "feed": "iex", "adjustment": "split"}}


def test_the_alpaca_closes_continue_a_crsp_tail_without_a_jump_and_both_sides_are_split_adjusted(db, upstream, crsp_tail, monkeypatch):
    _yahoo_rate_limited(monkeypatch)
    wrds_load.load_file(db, crsp_tail)
    report = alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA",))
    assert (report.rows, report.inserted, report.requests, report.unavailable) == (6, 6, 1, {})
    member = terminal.series(db, "equity:NVDA:close", "2y", _member())
    days = [(p.t.date().isoformat(), p.provenance.source, round(p.value, 3)) for p in member["points"]]
    assert days[0] == ("2024-06-06", "wrds-crsp", 121.09)               # CRSP: 1210.90 / 10, the same figure as Alpaca's adjusted bar
    assert days[1:4] == [("2025-12-29", "wrds-crsp", 190.1), ("2025-12-30", "wrds-crsp", 188.4), ("2025-12-31", "wrds-crsp", 186.5)]
    assert days[4] == ("2026-01-02", "alpaca-data", 188.825)             # the first Alpaca session follows the last CRSP one
    assert days[-1] == ("2026-01-09", "alpaca-data", 184.82)
    values = [v for _, _, v in days[1:]]
    assert max(abs(b / a - 1) for a, b in zip(values, values[1:])) < 0.03   # no cliff at the join: 186.5 -> 188.825 is +1.2 %
    assert all(a.t < b.t for a, b in zip(member["points"], member["points"][1:]))
    assert member["freshness"].sources == ("alpaca-data", "wrds-crsp")
    assert {d["source"]: d["licence"] for d in member["freshness"].source_details}["alpaca-data"] == licences.licence("alpaca-data")
    assert alpaca_data.parse_bars("NVDA", _recorded("bars-nvda-smci-2024-splits")["body"], datetime.now(UTC))[0].close == pytest.approx(121.09)
    assert member["points"][0].value == pytest.approx(121.09)


# -- load, reload, bounds ----------------------------------------------------------------------------

def test_a_second_load_writes_nothing_and_each_ticker_costs_one_request(db, upstream):
    first = alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA", "SMCI"))
    assert (first.rows, first.series, first.inserted, first.updated, first.unchanged) == (6, 1, 6, 0, 0)
    assert first.tickers == {"NVDA": 6} and first.unavailable == {} and first.requests == 2   # SMCI answered no bars: not a failure
    assert (first.first.isoformat(), first.last.isoformat()) == ("2026-01-02", "2026-01-09")
    assert len(upstream.requests) == 2 and db.query(Observation).count() == 6
    asked = upstream.asked("NVDA")[0]
    assert (asked["timeframe"], asked["adjustment"], asked["feed"], asked["start"], asked["end"], asked["limit"]) == ("1Day", "split", "iex", "2026-01-01", "2026-01-10", "10000")
    again = alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA", "SMCI"))
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 6) and db.query(Observation).count() == 6
    assert len(upstream.requests) == 2                                        # the window is cached: no second request
    assert alpaca_load.count_by_ticker(db) == {"NVDA": 6} and alpaca_load.newest_stored(db) == {"NVDA": date(2026, 1, 9)}
    # A corrected close is rewritten (a different window key reaches the upstream again):
    corrected = json.loads(json.dumps(upstream.bodies[("NVDA", "2026-01-01")]))
    corrected["bars"]["NVDA"][1]["c"] = 190.0
    upstream.bodies[("NVDA", "2026-01-01")] = corrected
    third = alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 11), tickers=("NVDA",))
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, 5) and db.query(Observation).count() == 6
    assert archive.archived_series(db, "equity:NVDA:close").points[1].value == 190.0
    dry = alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 12), tickers=("NVDA",), dry_run=True)
    assert dry.dry_run and (dry.inserted, dry.unchanged) == (0, 6) and db.query(Observation).count() == 6


def test_the_incremental_load_asks_only_for_the_days_after_the_newest_stored_bar(db, upstream):
    alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA",))
    upstream.requests.clear()
    report = alpaca_load.load_incremental(db, tickers=("NVDA", "SMCI"), today=date(2026, 1, 20))
    assert report.requests == 2 and report.rows == 0 and report.unavailable == {}
    assert upstream.asked("NVDA")[0]["start"] == "2026-01-10"                 # the day after the newest stored bar
    assert upstream.asked("SMCI")[0]["start"] == "2026-01-01"                 # nothing held: from the first day after CRSP ends
    assert all(q["end"] == "2026-01-20" for q in (upstream.asked("NVDA")[0], upstream.asked("SMCI")[0]))
    upstream.requests.clear()
    held = alpaca_load.load_incremental(db, tickers=("NVDA",), today=date(2026, 1, 9))
    assert held.requests == 0 and upstream.requests == []                      # already holds through today: no request


def test_delete_removes_every_alpaca_row_and_nothing_else(db, upstream, crsp_tail):
    wrds_load.load_file(db, crsp_tail)
    alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA",))
    assert db.query(Observation).count() == 10
    assert alpaca_load.delete_all(db, dry_run=True) == 6 and db.query(Observation).count() == 10
    assert alpaca_load.delete_all(db) == 6
    assert {str(r.provenance["source"]) for r in db.query(Observation).all()} == {"wrds-crsp"} and db.query(Observation).count() == 4
    assert alpaca_load.delete_all(db) == 0 and alpaca_load.count_by_ticker(db) == {}


def test_the_cli_loads_reports_and_deletes(db, upstream, monkeypatch, capsys):
    import infrastructure.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(alpaca_load, "TICKERS", ("NVDA",))
    assert alpaca_load.main(["load", "--start", "2026-01-01", "--end", "2026-01-10", "--dry-run"]) == 0 and db.query(Observation).count() == 0
    assert alpaca_load.main(["load", "--start", "2026-01-01", "--end", "2026-01-10"]) == 0 and db.query(Observation).count() == 6
    assert alpaca_load.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "6 inserted, 0 updated, 0 unchanged" in out and "NVDA: 6 (newest 2026-01-09)" in out
    assert alpaca_load.main(["delete", "--dry-run"]) == 0 and db.query(Observation).count() == 6
    assert alpaca_load.main(["delete"]) == 0 and db.query(Observation).count() == 0
    monkeypatch.setattr(alpaca_data.s, "PRODUCT_ALPACA_API_KEY", None)
    assert alpaca_load.main(["load"]) == 1


# -- failures: 429, auth, not configured -----------------------------------------------------------

def test_a_rate_limit_blocks_the_host_for_its_retry_after_and_an_auth_failure_is_a_reason_token(db, upstream, caplog):
    upstream.status, upstream.headers = 429, {"Retry-After": "120"}
    with caplog.at_level(logging.WARNING):
        result = alpaca_data.fetch_bars("NVDA", date(2026, 1, 1), date(2026, 1, 10))
        report = alpaca_load.load_window(db, {"NVDA": date(2026, 1, 1), "SMCI": date(2026, 1, 1)}, date(2026, 1, 10))
    assert not result.ok and result.reason == "http-429"
    assert report.unavailable == {"NVDA": "http-429", "SMCI": "http-429"} and report.inserted == 0 and db.query(Observation).count() == 0
    assert len(upstream.requests) == 1                                        # the host is blocked: SMCI was never sent
    assert "host paused 120s" in caplog.text
    assert KEY not in caplog.text and SECRET not in caplog.text               # the keys are never logged
    # A later call within the block is refused without a request, and nothing raises past the loader:
    assert alpaca_data.fetch_bars("CRWV", date(2026, 1, 1), date(2026, 1, 10)).reason == "http-429" and len(upstream.requests) == 1


def test_an_auth_failure_and_a_missing_key_fail_soft_without_a_write(db, upstream, monkeypatch):
    upstream.status = 401
    result = alpaca_data.fetch_bars("NVDA", date(2026, 1, 1), date(2026, 1, 10))
    assert not result.ok and result.reason == "http-401" and len(upstream.requests) == 1
    report = alpaca_load.load_window(db, {"NVDA": date(2026, 1, 1)}, date(2026, 1, 10))
    assert report.unavailable == {"NVDA": "http-401"} and db.query(Observation).count() == 0
    upstream.status = None
    monkeypatch.setattr(alpaca_data.s, "PRODUCT_ALPACA_SECRET_KEY", None)
    assert alpaca_data.configured() is False
    assert alpaca_data.fetch_bars("SMCI", date(2026, 1, 1)).reason == "not-configured" and len(upstream.requests) == 1
    assert alpaca_load.load_at_startup(db) is None and db.query(Observation).count() == 0
    assert alpaca_load.refresh_if_due(lambda: db) is None and len(upstream.requests) == 1


# -- startup order and the daily refresh -------------------------------------------------------------

def test_the_startup_task_runs_the_alpaca_load_after_the_wrds_loaders_and_before_the_refresher(db, upstream, monkeypatch, caplog):
    assert STARTUP_TASKS.index(load_research_archive) < STARTUP_TASKS.index(start_refresher)
    order: list[str] = []
    # A nested patch context: undoing it must not undo the `upstream` fixture's transport and dummy keys (a test never
    # reaches the network).
    with pytest.MonkeyPatch.context() as stubs:
        stubs.setattr(wrds_load, "load_secret_file_if_present", lambda db, path=None: order.append("wrds-crsp"))
        stubs.setattr(compustat_load, "load_secret_file_if_present", lambda db, path=None: order.append("wrds-compustat"))
        stubs.setattr(alpaca_load, "load_at_startup", lambda db: order.append("alpaca-data"))
        load_research_archive(db)
    assert order == ["wrds-crsp", "wrds-compustat", "alpaca-data"]
    assert _http.TRANSPORT is not None and alpaca_data.s.PRODUCT_ALPACA_API_KEY == KEY   # still the mock, still the dummy keys
    monkeypatch.setattr(alpaca_load, "TICKERS", ("NVDA", "SMCI"))
    monkeypatch.setattr(alpaca_load, "FIRST_DAY", date(2026, 1, 1))
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.alpaca_load"):
        report = alpaca_load.load_at_startup(db)
        again = alpaca_load.load_at_startup(db)
    assert report.inserted == 6 and report.requests == 2 and again.inserted == 0
    assert db.query(Observation).count() == 6 and "alpaca-data: loaded 6 bars over 1 series (2026-01-02 to 2026-01-09)" in caplog.text
    assert alpaca_load._refreshed_today(datetime.now(UTC).date()) is True      # the boot load counts as today's refresh


def test_the_refresher_hook_loads_once_a_day_across_calls_and_retries_a_failed_day(db, upstream, monkeypatch):
    monkeypatch.setattr(alpaca_load, "TICKERS", ("NVDA",))
    now = datetime(2026, 1, 12, 23, 30, tzinfo=UTC)
    first = alpaca_load.refresh_if_due(lambda: db, now=now)
    assert first is not None and first.inserted == 6 and len(upstream.requests) == 1
    assert alpaca_load.refresh_if_due(lambda: db, now=now) is None and len(upstream.requests) == 1   # done for the day
    # The marker is per UTC day; the next day loads again, from the day after the newest bar:
    assert alpaca_load.refresh_if_due(lambda: db, now=datetime(2026, 1, 13, 0, 5, tzinfo=UTC)) is not None
    assert upstream.asked("NVDA")[-1]["start"] == "2026-01-10" and len(upstream.requests) == 2
    # A day whose load had a failed ticker is not marked done; it is retried after the retry clock, not every tick:
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    alpaca_load.reset_refresh_state()
    upstream.status, upstream.headers = 429, {"Retry-After": "1"}
    failed = alpaca_load.refresh_if_due(lambda: db, now=datetime(2026, 1, 14, 1, tzinfo=UTC))
    assert failed is not None and failed.unavailable == {"NVDA": "http-429"}
    assert alpaca_load._refreshed_today(date(2026, 1, 14)) is False
    assert alpaca_load.refresh_if_due(lambda: db, now=datetime(2026, 1, 14, 1, 1, tzinfo=UTC)) is None          # inside the retry clock
    assert alpaca_load.refresh_if_due(None, now=datetime(2026, 1, 14, 1, 1, tzinfo=UTC)) is None                 # no session factory: nothing to do


def test_the_refresher_loop_calls_the_daily_hook_with_its_session_factory(monkeypatch):
    calls: list[object] = []
    stop = threading.Event()

    def hook(session_factory):
        calls.append(session_factory)
        stop.set()
        return None

    monkeypatch.setattr(alpaca_load, "refresh_if_due", hook)
    monkeypatch.setattr(refresher, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(refresher, "recompose_due", lambda session_factory: 0)
    monkeypatch.setattr(_http, "refresh_due", lambda pool=None: 0)
    factory = object()
    thread = threading.Thread(target=refresher._run, args=(stop, factory, lambda: None, False), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and calls == [factory]


# -- who sees the points: the series and the company's latest close -----------------------------------

def test_the_series_and_the_company_close_reach_signed_in_readers_and_anonymous_readers_keep_the_sample(db, upstream, crsp_tail, monkeypatch):
    _yahoo_rate_limited(monkeypatch)
    wrds_load.load_file(db, crsp_tail)
    alpaca_load.load_range(db, date(2026, 1, 1), date(2026, 1, 10), tickers=("NVDA",))
    anonymous = terminal.series(db, "equity:NVDA:close", "2y", None)
    assert {p.provenance.kind for p in anonymous["points"]} == {"sample"} and "alpaca-data" not in anonymous["freshness"].sources
    member = terminal.series(db, "equity:NVDA:close", "2y", _member())
    assert [p.provenance.source for p in member["points"]] == ["wrds-crsp"] * 4 + ["alpaca-data"] * 6
    # The company's latest close, with Yahoo rate-limited: the newest Alpaca close for a member, the sample for anyone else.
    detail = terminal.company(db, "NVDA", _member())
    assert detail["unavailable"] == {"source": "yahoo-finance", "reason": "http-429"} and detail["latest_bar"] is None
    assert detail["latest_close"].provenance.source == "alpaca-data" and detail["latest_close"].value == pytest.approx(184.82)
    assert detail["latest_close"].t == datetime(2026, 1, 9, 21, 0, tzinfo=UTC)
    assert terminal.company(db, "NVDA", None)["latest_close"].provenance.kind == "sample"
    # The archive's own reads honour the gate, so no other composition leaks a point:
    assert archive.archived_series(db, "equity:NVDA:close", public_only=True) is None
    assert archive.latest_before(db, "equity:NVDA:close", datetime(2030, 1, 1, tzinfo=UTC), public_only=True) is None

    def client(user):
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(router)
        app.dependency_overrides[deps.get_db] = lambda: db
        app.dependency_overrides[deps.get_current_user_or_none] = lambda: user
        return TestClient(app)

    anon = client(None).get("/gpu/series", params={"id": "equity:NVDA:close", "range": "2y"})
    assert anon.status_code == 200 and {p["provenance"]["kind"] for p in anon.json()["points"]} == {"sample"}
    signed = client(_member()).get("/gpu/series", params={"id": "equity:NVDA:close", "range": "2y"}, headers={"Authorization": "Bearer x"})
    body = signed.json()
    assert signed.status_code == 200 and body["freshness"]["sources"] == ["alpaca-data", "wrds-crsp"]
    assert body["points"][-1]["t"] == "2026-01-09T21:00:00Z" and body["points"][-1]["provenance"]["source"] == "alpaca-data"
    assert body["points"][3]["t"] == "2025-12-31T21:00:00Z" and body["points"][4]["t"] == "2026-01-02T21:00:00Z"
    company = client(_member()).get("/companies/NVDA", headers={"Authorization": "Bearer x"}).json()
    assert company["latestClose"]["provenance"]["source"] == "alpaca-data" and company["latestClose"]["value"] == pytest.approx(184.82)
    assert client(None).get("/companies/NVDA").json()["latestClose"]["provenance"]["kind"] == "sample"
