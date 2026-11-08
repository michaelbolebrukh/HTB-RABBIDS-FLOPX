"""Source honesty (falsifier wave V6 and V1 F7, F12): licences, observation times, rate limits, provenance per row.

Every expectation here is stated independently of the code under test: the licence strings are
written out, the close times are worked out from the exchange session by hand, and the network is
a MockTransport (no test reaches an upstream).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.exceptions import UnknownEntityError
from product.flopx import archive, domain, terminal
from product.flopx.provenance import live
from product.flopx.sources import _http, akash, available, energy, kalshi, licences, news, polymarket, runpod, sec, unavailable, vast, wikidata, yahoo
from product.models import Observation

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
NEXT_PWA_COPY = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
UTC = timezone.utc


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    # A net under the seams: anything not replaced below fails rather than reaching an upstream.
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: pytest.fail(f"network call to {request.url.host}")))


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


# -- the licence table (V6 #1, #2, #3, #10) ----------------------------------------------------

def test_the_licence_table_decides_use_per_source():
    # Vast.ai (founder decision G9-b): called and shown, never archived, never in an index basket.
    vast_row = licences.policy("vast-ai")
    assert vast_row["use"] == "display-not-index" and licences.enabled("vast-ai") is True
    assert licences.archivable("vast-ai") is False and licences.in_index("vast-ai") is False
    assert vast_row["attribution"] == "Vast.ai asks: a current market reading, not an index or benchmark"
    assert "G9-b" in vast_row["note"] and "index or benchmark" in vast_row["note"] and "after the hackathon" in vast_row["note"]
    assert "vast-ai" in {row["id"] for row in licences.rows()}                     # listed on the sources page
    assert licences.in_index("runpod") is True and licences.in_index("flopx-sample-v1") is True
    assert licences.policy("yahoo-finance")["use"] == "display-only" and licences.archivable("yahoo-finance") is False
    assert licences.enabled("yahoo-finance") is True
    for source in ("polymarket-gamma", "polymarket-clob", "runpod"):
        row = licences.policy(source)
        assert row["use"] == "display-and-archive" and row["asserted"] is True and "Risk recorded" in row["note"]
    assert licences.policy("kalshi:KXPOWERKWH")["id"] == "kalshi"                  # a per-series id resolves to its source
    assert licences.policy("rss:nvidia")["id"] == "rss"
    assert licences.enabled("a-source-nobody-decided") is False                     # no row, no use


def test_adapters_carry_the_tables_strings_with_the_asserted_caveat_where_unverified():
    assert yahoo.LICENCE == "Unofficial endpoint; the provider's terms restrict redistribution. Shown pending a licensed price feed."
    assert runpod.LICENCE == "Public GraphQL list prices (terms asserted, not verified)"
    assert polymarket.LICENCE == "Public market data API (terms asserted, not verified)"
    assert energy.ENERGY_CHARTS_LICENCE == "CC BY 4.0"                              # measured in the body: no caveat
    assert energy.ELEXON_ATTRIBUTION == "Contains BMRS data © Elexon Limited copyright and database right 2026"
    assert licences.attribution("energy-charts") == "Bundesnetzagentur | SMARD.de, via Energy-Charts (Fraunhofer ISE), CC BY 4.0"


def test_the_frontend_reads_a_byte_identical_copy_of_the_table():
    if not NEXT_PWA_COPY.exists():
        pytest.skip("the next-pwa tree is not in this checkout")
    assert NEXT_PWA_COPY.read_bytes() == licences.TABLE_PATH.read_bytes()


def test_the_archive_never_stores_a_display_only_source(db):
    t = datetime(2026, 11, 6, 21, tzinfo=UTC)
    bar = domain.EquityBar(ticker="NVDA", t=t, open=1.0, high=1.0, low=1.0, close=186.64, volume=1,
                           provenance=live("yahoo-finance", t, licence=yahoo.LICENCE))
    stored = archive.record_results(db, [available("yahoo-finance", [bar], bar.provenance)], archive.observations_from_bars, label="closes")
    assert stored == 0 and db.query(Observation).count() == 0
    offer = domain.GpuOffer(id="runpod-x", provider_id="runpod", region_id="global", gpu_model_id="h100-sxm-80gb", gpu_count=1,
                            price_per_gpu_hour_usd=2.69, availability="available", interruptible=False, contract="on-demand",
                            provenance=live("runpod", t, licence=runpod.LICENCE))
    assert archive.record_results(db, [available("runpod", [offer], offer.provenance)], archive.observations_from_offers, label="offers") == 2


# -- observation times (V6 #4, #5, #6, #14) ------------------------------------------------------

def _yahoo_body() -> dict:
    return json.loads((RECORDED / "yahoo" / "chart-nvda-1y.json").read_text(encoding="utf-8"))


def test_a_daily_bar_is_stamped_at_the_session_close_never_the_open():
    fetched = datetime(2026, 11, 7, 20, tzinfo=UTC)
    bars, _ = yahoo._parse("NVDA", _yahoo_body(), fetched, "1d")
    by_close = {b.t: b for b in bars}
    # Yahoo keys Friday 6 November 2026 at 14:30 UTC (the open); the close is 16:00 New York = 21:00 UTC.
    friday = by_close[datetime(2026, 11, 6, 21, 0, tzinfo=UTC)]
    assert friday.close == pytest.approx(186.64, abs=0.01) and friday.provenance.observed_at == friday.t
    # In June New York is on daylight time: the 13:30 UTC open closes at 20:00 UTC.
    assert datetime(2026, 6, 16, 20, 0, tzinfo=UTC) in by_close
    assert all(b.t.hour in (20, 21) and b.t.minute == 0 for b in bars)


def test_a_session_still_trading_is_stamped_at_its_last_trade():
    body = _yahoo_body()
    meta = body["chart"]["result"][0]["meta"]
    meta["regularMarketTime"] = 1793975400 + 3600          # one hour into Friday's session
    bars, _ = yahoo._parse("NVDA", body, datetime(2026, 11, 6, 15, 31, tzinfo=UTC), "1d")
    assert bars[-1].t == datetime(2026, 11, 6, 15, 30, tzinfo=UTC)
    assert bars[-2].t == datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


def test_intraday_and_weekly_bars_close_at_their_end():
    assert yahoo.bar_close_times([1_000_000, 1_000_300], "5m", {}) == [1_000_300, 1_000_600]
    assert yahoo.bar_close_times([0, 604_800], "1wk", {}) == [604_800, 1_209_600]


def test_energy_keeps_only_slots_already_in_force_at_the_fetch():
    body = json.loads((RECORDED / "energy" / "energy-charts-de-lu.json").read_text(encoding="utf-8"))["body"]
    fetched = datetime(2026, 11, 6, 1, 20, tzinfo=UTC)       # the body runs 23:00 to 02:45 in 15-minute slots
    points = energy.parse_energy_charts(body, "DE-LU", fetched)
    assert len(points) == 10 and points[-1].t == datetime(2026, 11, 6, 1, 15, tzinfo=UTC)
    assert points[-1].value == pytest.approx(body["price"][9])
    assert all(p.t <= fetched and p.provenance.observed_at == p.t for p in points)


def test_upstream_times_leave_the_seam_in_utc():
    parsed = _http.rfc822("Thu, 05 Nov 2026 07:00:00 -0500")
    assert parsed == datetime(2026, 11, 5, 12, 0, tzinfo=UTC) and parsed.isoformat().endswith("+00:00")
    assert _http.iso("2026-11-05T07:00:00-05:00").isoformat() == "2026-11-05T12:00:00+00:00"


# -- rate limits (V6 #11) ------------------------------------------------------------------------

def test_retry_after_blocks_the_whole_host_and_is_capped_at_an_hour(monkeypatch):
    calls = []

    def limited(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "1000"})

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(limited))
    first = _http.fetch_json("wikidata", "https://query.wikidata.org/sparql", key="t:wd:1", ttl_seconds=900)
    second = _http.fetch_json("wikidata", "https://query.wikidata.org/other", key="t:wd:2", ttl_seconds=900)
    assert first.reason == "http-429" and first.ttl_seconds == 1000
    assert second.reason == "http-429" and calls == ["/sparql"]       # the second path never reached the host
    assert _http.retry_after_seconds("100000") == 3600
    assert _http.retry_after_seconds("Sat, 07 Nov 2026 20:10:00 GMT", now=datetime(2026, 11, 7, 20, 0, tzinfo=UTC)) == 600


def test_a_yahoo_429_without_retry_after_pauses_ten_minutes(monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(429)))
    result = _http.fetch_json("yahoo-finance", "https://query1.finance.yahoo.com/v8/finance/chart/NVDA", key="t:y:1", ttl_seconds=300)
    assert result.reason == "http-429" and result.ttl_seconds == 600


def test_calls_to_a_spaced_host_wait_their_turn(monkeypatch):
    slept = []
    monkeypatch.setattr(_http.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(_http, "_last_call", {})
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
    for i in range(3):
        _http.fetch_json("yahoo-finance", f"https://query1.finance.yahoo.com/v8/finance/chart/T{i}", key=f"t:y:s{i}", ttl_seconds=300)
    assert len(slept) == 2 and all(0.9 <= s <= 1.0 for s in slept)   # three calls inside a second: two spaced waits


# -- per-request ledger, freshness and unavailable (V6 #7, #8, #14; V1 F12) --------------------------

@pytest.fixture
def sources_down(monkeypatch):
    """Every upstream refuses, as on V6's 8031 backend (Vast.ai included: it is called again under G9-b)."""
    monkeypatch.setattr(vast, "fetch_gpu_offers", lambda: unavailable("vast-ai", "network-error"))
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: unavailable("runpod", "network-error"))
    monkeypatch.setattr(akash, "fetch_gpu_offers", lambda: unavailable("akash", "network-error"))
    monkeypatch.setattr(polymarket, "fetch_prediction_markets", lambda: unavailable("polymarket-gamma", "network-error"))
    monkeypatch.setattr(kalshi, "fetch_series", lambda series: unavailable(f"kalshi:{series}", "network-error"))
    monkeypatch.setattr(news, "fetch_hn", lambda query=None, limit=20: unavailable("hacker-news", "network-error"))
    monkeypatch.setattr(news, "fetch_rss", lambda key, limit=10: unavailable(f"rss:{key}", "network-error"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "network-error"))
    monkeypatch.setattr(energy, "fetch_energy_charts", lambda zone="DE-LU", days=1: unavailable("energy-charts", "network-error"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: unavailable("elexon-bmrs", "network-error"))
    monkeypatch.setattr(wikidata, "fetch_facilities", lambda: unavailable("wikidata", "http-429"))
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "http-429"))


def test_a_live_only_series_id_answers_empty_and_names_its_failed_source(db, sources_down):
    body = terminal.series(db, "energy:DE-LU:day-ahead", None)
    assert body["points"] == [] and body["unit"] == "EUR/MWh"
    assert body["freshness"].unavailable == ({"source": "energy-charts", "reason": "network-error"},)


def test_a_live_only_series_merges_live_points_with_the_archive(db, monkeypatch, sources_down):
    t = datetime(2026, 11, 7, 19, 45, tzinfo=UTC)
    point = domain.PriceObservation(series_id="energy:GB:market-index", t=t, value=88.0, unit="GBP/MWh", provenance=live("elexon-bmrs", t, licence="x"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: available("elexon-bmrs", [point], point.provenance))
    first = terminal.series(db, "energy:GB:market-index", None)
    assert [p.value for p in first["points"]] == [88.0] and first["points"][0].provenance.kind == "live"
    assert [d["source"] for d in first["freshness"].source_details] == ["elexon-bmrs"]


def test_unknown_anomaly_ids_say_which_sources_did_not_answer(db, sources_down):
    with pytest.raises(UnknownEntityError) as refused:
        terminal.anomaly_detail(db, "anomaly:price-dispersion:us-tx:h100-sxm-80gb")
    assert {"source": "runpod", "reason": "network-error"} in refused.value.details["unavailable"]


def test_the_same_failure_is_named_once():
    ledger = terminal.Ledger()
    ledger.take([unavailable("sec-edgar", "network-error"), unavailable("sec-edgar", "network-error"), unavailable("sec-edgar", "http-403")])
    assert ledger.unavailable == [{"source": "sec-edgar", "reason": "network-error"}, {"source": "sec-edgar", "reason": "http-403"}]


def test_authored_changes_read_sample_and_end_at_their_snapshot_not_the_fetch(db, monkeypatch, sources_down):
    # Live asks answer, but the archive has already seen them (the same observation): nothing live moved.
    t = datetime(2026, 11, 7, 19, 47, 26, tzinfo=UTC)
    asks = [domain.GpuOffer(id="runpod-1", provider_id="runpod", region_id="global", gpu_model_id="h100-sxm-80gb", gpu_count=1,
                            price_per_gpu_hour_usd=2.69, availability="available", interruptible=False, contract="on-demand",
                            provenance=live("runpod", t, licence="x"))]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", asks, live("runpod", t)))
    terminal.changes(db, None)                              # archives the asks
    body = terminal.changes(db, None)
    assert body["changes"] and all(c.provenance.kind == "sample" for c in body["changes"])
    assert body["until"] == datetime(2026, 11, 6, 21, 0, tzinfo=UTC)          # the authored "after" snapshot, never 19:47:26
    assert "runpod" not in body["freshness"].sources and body["freshness"].source_details == ()


def test_the_single_index_read_carries_freshness(db, sources_down):
    body = terminal.index(db, "h100-sxm-80gb")
    assert body["provenance"].input_kinds == ("sample",)
    assert {u["source"] for u in body["freshness"].unavailable} == {"runpod", "akash", "vast-ai"}


# -- composition (V6 #12) ---------------------------------------------------------------------------

def test_the_overview_runs_its_upstream_stages_as_one_fan_out(db, monkeypatch, sources_down):
    fan_outs = []
    real = terminal._gather
    monkeypatch.setattr(terminal, "_gather", lambda calls: fan_outs.append(len(calls)) or real(calls))
    terminal.overview(db, None)
    # 3 marketplaces (Vast.ai called again under G9-b) + 2 power + 8 tickers + 5 market series + 5 news feeds, all at once:
    assert fan_outs == [3 + 2 + 8 + 5 + 5]


def test_anomalies_and_signals_compose_once_per_snapshot_window(db, monkeypatch, sources_down):
    built = []
    real = terminal.InferredInputs

    def counting(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(terminal, "InferredInputs", counting)
    terminal.anomalies_view(db, None, None, None)
    terminal.signals_view(db, None, None)
    terminal.anomalies_view(db, "us-tx", None, None)
    assert len(built) == 1


def test_evaluation_names_its_failed_inputs_and_only_the_sources_it_read(db, monkeypatch, sources_down):
    # Live asks answer for one model the harness does not read: they must not appear as its sources (V6 #7).
    t = datetime(2026, 11, 7, 19, 47, tzinfo=UTC)
    ask = domain.GpuOffer(id="runpod-l40s", provider_id="runpod", region_id="global", gpu_model_id="l40s-48gb", gpu_count=1,
                          price_per_gpu_hour_usd=0.86, availability="available", interruptible=False, contract="on-demand",
                          provenance=live("runpod", t, licence="x"))
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", [ask], live("runpod", t)))
    body = terminal.evaluation_view(db, "CRWV", None)
    fresh = body["freshness"]
    assert "runpod" not in fresh.sources and fresh.source_details == ()
    # Every source the harness asked and did not get is named (V6 #8): no silent downgrade to sample.
    assert {"source": "energy-charts", "reason": "network-error"} in fresh.unavailable
    assert {"source": "akash", "reason": "network-error"} in fresh.unavailable


def test_a_caller_queued_behind_a_refused_request_does_not_fire(monkeypatch):
    # Measured on a live run before the fix: the first ticker got 429 and the second went out anyway.
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    monkeypatch.setattr(_http.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(_http, "_last_call", {})

    def limited(request):
        calls.append(request.url.path)
        return httpx.Response(429)

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(limited))
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda t: _http.fetch_json("yahoo-finance", url.format(t), key=f"t:q:{t}", ttl_seconds=300), ["A", "B", "C", "D"]))
    assert len(calls) == 1 and all(r.reason == "http-429" for r in results)
