"""Every live adapter parses its recorded upstream response and degrades to `unavailable`.

The responses under `tests/fixtures/recorded/<source>/` were fetched once from
the real hosts (2026-11-07, one request each) and trimmed without changing
shape. They are served here through `httpx.MockTransport` on the adapters'
shared transport seam; no test opens a network connection. Each adapter must
return `kind: live` records with the upstream's own observation time (or the
response Date header, with the note saying so), a licence, and on any failure
an `unavailable` result with a reason token, never an exception.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from product.flopx.sources import _http, akash, energy, kalshi, news, polymarket, runpod, sec, vast, wikidata

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
RECORDED_DATE = datetime(2026, 11, 7, 15, 51, 8, tzinfo=timezone.utc)


def _load(source: str, name: str) -> dict:
    return json.loads((RECORDED / source / f"{name}.json").read_text(encoding="utf-8"))


def _route(request: httpx.Request) -> tuple[str, str] | None:
    host, path, q = request.url.host, request.url.path, dict(request.url.params)
    table = {
        ("console.vast.ai", "/api/v0/bundles/"): ("vast", "bundles"),
        ("api.runpod.io", "/graphql"): ("runpod", "gpu-types"),
        ("console-api.akash.network", "/v1/gpu-prices"): ("akash", "gpu-prices"),
        ("gamma-api.polymarket.com", "/public-search"): ("polymarket", "public-search"),
        ("clob.polymarket.com", "/prices-history"): ("polymarket", "prices-history"),
        ("api.energy-charts.info", "/price"): ("energy", "energy-charts-de-lu"),
        ("data.elexon.co.uk", "/bmrs/api/v1/balancing/pricing/market-index"): ("energy", "elexon-market-index"),
        ("hn.algolia.com", "/api/v1/search_by_date"): ("news", "hn-search-by-date"),
        ("nvidianews.nvidia.com", "/releases.xml"): ("news", "rss-nvidia"),
        ("investors.coreweave.com", "/rss/pressrelease.aspx"): ("news", "rss-coreweave"),
        ("investors.corescientific.com", "/news-events/press-releases/rss"): ("news", "rss-corescientific"),
        ("ir.applieddigital.com", "/news-events/press-releases/rss"): ("news", "rss-applieddigital"),
        ("query.wikidata.org", "/sparql"): ("wikidata", "sparql-data-centres"),
    }
    if (host, path) in table:
        return table[(host, path)]
    if host == "api.elections.kalshi.com":
        if path.endswith("/candlesticks"):
            return ("kalshi", "candlesticks")
        series = q.get("series_ticker", "")
        return ("kalshi", {"KXUSDCCAPACITY": "markets-kxusdccapacity", "KXPOWERKWH": "markets-kxpowerkwh"}.get(series, "markets-kxutilitypjmwest-empty"))
    if host == "data.sec.gov":
        if path.startswith("/submissions/"):
            return ("sec", "submissions-nvda")
        if path.startswith("/api/xbrl/companyfacts/"):
            return ("sec", "companyfacts-crwv")
        if path.startswith("/api/xbrl/frames/"):
            return ("sec", "frames-capex-cy2025q2")
    return None


def recorded_handler(request: httpx.Request) -> httpx.Response:
    hit = _route(request)
    if hit is None:
        return httpx.Response(404, text="unrouted in test")
    rec = _load(*hit)
    headers = {"Date": rec["date"], "Content-Type": rec["contentType"]}
    body = rec["body"]
    return httpx.Response(rec["status"], headers=headers, text=body) if isinstance(body, str) else httpx.Response(rec["status"], headers=headers, json=body)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    # The adapters cache through the process singleton; every test starts from an empty store.
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))


@pytest.fixture
def recorded(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return recorded_handler(request)

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def _assert_live(result, source_prefix: str):
    assert result.ok, result.reason
    assert result.records and result.provenance.kind == "live" and result.provenance.source.startswith(source_prefix)
    assert result.provenance.licence
    for record in result.records:
        assert record.provenance.kind == "live"
        assert record.provenance.fetched_at is not None


# -- GPU asks ------------------------------------------------------------------------

def test_vast_maps_exact_variants_only_and_stamps_the_date_header(recorded):
    result = vast.fetch_gpu_offers()
    _assert_live(result, "vast-ai")
    models = {o.gpu_model_id for o in result.records}
    assert models <= {"h100-sxm-80gb", "h200-sxm-141gb", "b200-sxm-192gb", "a100-sxm-80gb", "l40s-48gb", "rtx-4090-24gb"}
    assert "h100-sxm-80gb" in models and "rtx-4090-24gb" in models
    # The recorded body held H100 PCIe/NVL and H200 NVL rows: not exact variants, so not mapped.
    assert not any("pcie" in o.id or "nvl" in o.id for o in result.records)
    first = result.records[0]
    assert first.provenance.observed_at == RECORDED_DATE and _http.DATE_HEADER_NOTE in first.provenance.note
    texas = [o for o in result.records if o.region_id == "us-tx"]
    assert texas and all(o.price_per_gpu_hour_usd > 0 for o in result.records)
    assert len(recorded) == 2  # the two fixed queries, never a pagination loop


def test_vast_per_gpu_price_divides_the_bundle_price():
    raw = {"id": 1, "gpu_name": "H100 SXM", "num_gpus": 8, "gpu_ram": 81559, "dph_total": 24.0, "rentable": True, "rented": False, "geolocation": "Virginia, US", "reliability2": 0.99}
    offers = vast.parse({"offers": [raw]}, RECORDED_DATE, RECORDED_DATE)
    assert len(offers) == 1 and offers[0].price_per_gpu_hour_usd == 3.0 and offers[0].region_id == "us-va" and offers[0].gpu_count == 8
    # A 40 GB A100 is not the 80 GB model:
    assert vast.parse({"offers": [dict(raw, gpu_name="A100 SXM4", gpu_ram=40960)]}, RECORDED_DATE, RECORDED_DATE) == []


def test_runpod_yields_secure_and_community_list_prices(recorded):
    result = runpod.fetch_gpu_offers()
    _assert_live(result, "runpod")
    by_id = {o.id: o for o in result.records}
    assert by_id["runpod-nvidia-b200-secure"].price_per_gpu_hour_usd == 7.99
    assert by_id["runpod-nvidia-b200-community"].price_per_gpu_hour_usd == 5.98
    assert by_id["runpod-nvidia-h100-80gb-hbm3-secure"].gpu_model_id == "h100-sxm-80gb"
    assert all(o.region_id == "global" and o.contract == "on-demand" for o in result.records)
    assert "stock status not published keyless" in by_id["runpod-nvidia-h100-pcie-secure"].provenance.note if "runpod-nvidia-h100-pcie-secure" in by_id else True
    assert recorded[0].method == "POST" and "authorization" not in {k.lower() for k in recorded[0].headers}


def test_akash_median_per_model_with_availability(recorded):
    result = akash.fetch_gpu_offers()
    _assert_live(result, "akash")
    h100 = next(o for o in result.records if o.gpu_model_id == "h100-sxm-80gb")
    assert h100.price_per_gpu_hour_usd == 2.86 and h100.gpu_count == 12 and h100.availability == "available"
    assert "median of 32 listed GPUs" in h100.provenance.note
    rtx = next(o for o in result.records if o.gpu_model_id == "rtx-4090-24gb")
    assert rtx.availability == "unavailable"


# -- prediction markets ----------------------------------------------------------------

def test_polymarket_keeps_active_ornn_settled_gpu_markets_with_real_update_times(recorded):
    result = polymarket.fetch_prediction_markets()
    _assert_live(result, "polymarket-gamma")
    assert all(m.id.startswith("polymarket:") and m.venue == "polymarket" for m in result.records)
    assert all("ornn" in (m.resolution_rules or "").lower() for m in result.records)
    assert all(abs(sum(o.price for o in m.outcomes) - 1.0) < 0.02 for m in result.records)
    # The closed July markets in the recorded search are excluded:
    assert not any("July" in m.question for m in result.records)
    newest = max(m.last_updated for m in result.records)
    assert newest.year == 2026 and newest != RECORDED_DATE  # the body's updatedAt, not the Date header
    assert result.records == sorted(result.records, key=lambda m: m.volume_24h or 0, reverse=True)


def test_polymarket_history_uses_the_yes_token_once(recorded):
    history = polymarket.fetch_market_history("polymarket:6658648")
    assert history.ok and history.source == "polymarket-clob"
    assert [p.value for p in history.records][:2] == [0.495, 0.215]
    assert all(p.unit == "probability" and p.series_id == "market:polymarket:6658648:yes" for p in history.records)
    clob = [r for r in recorded if r.url.host == "clob.polymarket.com"]
    assert len(clob) == 1 and clob[0].url.params["market"].startswith("99916716908319740055732204057967759955225104782352905293991055601938387199132")
    assert polymarket.fetch_market_history("polymarket:does-not-exist").reason == "unknown-market"


def test_kalshi_series_parse_and_an_empty_series_is_unavailable_with_reason(recorded):
    capacity = kalshi.fetch_series("KXUSDCCAPACITY")
    _assert_live(capacity, "kalshi")
    m = next(x for x in capacity.records if x.id == "kalshi:KXUSDCCAPACITY-27MAR05-T65.0")
    assert m.outcomes[0].price == 0.19 and m.outcomes[1].price == 0.81 and m.outcomes[0].label == "Yes (Above 65.0 GW)"
    assert m.last_updated == datetime(2026, 6, 27, 20, 30, 0, 785653, tzinfo=timezone.utc)
    assert m.resolution_rules.startswith("If the Americas operational data center capacity")
    pjm = kalshi.fetch_series("KXUTILITYPJMWEST")
    assert not pjm.ok and pjm.unavailable == {"source": "kalshi:KXUTILITYPJMWEST", "reason": "no-open-markets"}
    results = kalshi.fetch_prediction_markets()
    assert [r.ok for r in results] == [True, True, False, False]
    history = kalshi.fetch_market_history("kalshi:KXUSDCCAPACITY-27MAR05-T65.0")
    assert history.ok and history.records[-1].value == 0.19 and len(history.records) == 12
    assert [r.url.path for r in recorded if r.url.path.endswith("candlesticks")] == ["/trade-api/v2/series/KXUSDCCAPACITY/markets/KXUSDCCAPACITY-27MAR05-T65.0/candlesticks"]


# -- SEC ------------------------------------------------------------------------------

def test_sec_sends_the_declared_user_agent_and_carries_the_filed_date(recorded):
    result = sec.fetch_fundamentals("1769628", "CRWV")
    _assert_live(result, "sec-edgar")
    assert all("FLOPX research terminal hello@flopx.vercel.app" == r.headers["user-agent"] for r in recorded)
    concepts = {f.concept for f in result.records}
    assert concepts == {"revenue", "capex", "ppe", "netIncome", "rpo"}
    rpo = max((f for f in result.records if f.concept == "rpo"), key=lambda f: f.period_end)
    assert rpo.value == 103_700_000_000 and rpo.period_end == "2026-06-30" and rpo.filed_at == "2026-08-12"
    assert rpo.provenance.observed_at == datetime(2026, 8, 12, tzinfo=timezone.utc)
    assert "filed date is the availability date" in rpo.provenance.note
    # No look-ahead: the provenance observation time is never before the filing:
    assert all(f.provenance.observed_at.date().isoformat() == f.filed_at for f in result.records)
    assert len({(f.concept, f.period_end) for f in result.records}) == len(result.records)


def test_sec_filing_feed_is_verified_and_skips_insider_forms(recorded):
    result = sec.fetch_filings("1045810", "NVDA")
    _assert_live(result, "sec-edgar")
    assert all(i.classification == "verified" for i in result.records)
    assert all(i.kind in {"announcement", "earnings"} for i in result.records)
    assert not any("Form 4" in i.headline or " 4:" in i.headline for i in result.records)
    first = result.records[0]
    assert first.id.startswith("sec:0001045810-26-") and first.evidence[0].kind == "filing" and first.entities.tickers == ["NVDA"]
    assert first.evidence[0].href.startswith("https://www.sec.gov/Archives/edgar/data/1045810/")


def test_sec_frame_keeps_only_terminal_companies_and_periods_are_computed(recorded):
    result = sec.fetch_frame("capex", "CY2025Q2")
    assert result.ok and {f.ticker for f in result.records} == {"CRWV"}
    assert result.records[0].filed_at == result.records[0].period_end == "2025-06-30"
    assert sec.latest_closed_quarter(datetime(2026, 11, 7).date()) == "CY2026Q2"
    assert sec.latest_closed_quarter(datetime(2026, 2, 1).date()) == "CY2025Q3"
    assert sec.latest_closed_quarter(datetime(2026, 5, 20).date()) == "CY2026Q1"


# -- energy ---------------------------------------------------------------------------

def test_energy_charts_carries_the_cc_by_attribution_and_its_own_timestamps(recorded):
    result = energy.fetch_energy_charts("DE-LU")
    _assert_live(result, "energy-charts")
    first = result.records[0]
    assert first.series_id == "energy:DE-LU:day-ahead" and first.unit == "EUR/MWh" and first.value == 247.25
    assert first.t == datetime.fromtimestamp(1793919600, tz=timezone.utc) and first.provenance.observed_at == first.t
    assert "CC BY 4.0" in first.provenance.note and "SMARD.de" in first.provenance.note and first.provenance.licence == "CC BY 4.0"
    assert result.provenance.note.startswith("CC BY 4.0")


def test_elexon_half_hourly_prices_in_gbp(recorded):
    result = energy.fetch_elexon()
    _assert_live(result, "elexon-bmrs")
    assert all(p.unit == "GBP/MWh" and p.series_id == "energy:GB:market-index" for p in result.records)
    assert result.records == sorted(result.records, key=lambda p: p.t)
    assert "Elexon" in result.records[0].provenance.note


# -- news ---------------------------------------------------------------------------------

def test_hn_items_are_interpretation_and_rss_items_are_verified(recorded):
    hn = news.fetch_hn()
    _assert_live(hn, "hacker-news")
    assert all(i.classification == "interpretation" for i in hn.records)
    assert all(i.evidence[1].href.startswith("https://news.ycombinator.com/item?id=") for i in hn.records)
    assert hn.records[0].published_at == datetime(2026, 11, 6, 19, 5, 15, tzinfo=timezone.utc)
    for key, ticker in (("nvidia", "NVDA"), ("coreweave", "CRWV"), ("corescientific", "CORZ"), ("applieddigital", "APLD")):
        rss = news.fetch_rss(key)
        _assert_live(rss, f"rss:{key}")
        assert all(i.classification == "verified" and ticker in i.entities.tickers for i in rss.records)
        assert rss.records == sorted(rss.records, key=lambda i: i.published_at, reverse=True)
    corz = news.fetch_rss("corescientific").records[0]
    assert corz.kind == "earnings" and "Third Quarter 2026" in corz.headline
    assert news.fetch_rss("coreweave").records[0].published_at == datetime(2026, 10, 27, 20, 5, tzinfo=timezone.utc)


def test_rss_with_a_dtd_is_refused_not_parsed():
    hostile = '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss><channel><item><title>&x;</title><pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>'
    assert news.parse_rss(hostile, "nvidia", RECORDED_DATE) == []
    assert news.parse_rss("not xml at all", "nvidia", RECORDED_DATE) == []


# -- wikidata -----------------------------------------------------------------------------

def test_wikidata_facilities_are_reported_status_cc0_and_geocoded(recorded):
    result = wikidata.fetch_facilities()
    _assert_live(result, "wikidata")
    pearl = next(f for f in result.records if f.id == "wikidata:Q227873")
    assert (pearl.lat, pearl.lon) == (40.7108, -74.0012) and pearl.status == "reported" and pearl.region_id == "us"
    # The licence string comes from licences.json; Wikidata's CC0 was not read from its policy page, so it carries the caveat:
    assert pearl.provenance.licence == "CC0 (terms asserted, not verified)" and "notable" in result.provenance.note
    assert recorded[0].headers["accept"] == "application/sparql-results+json"


# -- failure handling, shared seam ----------------------------------------------------------

def test_http_failures_become_unavailable_and_are_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(503, text="down")))
    with caplog.at_level(logging.WARNING):
        result = akash.fetch_gpu_offers()
    assert not result.ok and result.unavailable == {"source": "akash", "reason": "http-503"}
    assert any("akash: HTTP 503" in r.message for r in caplog.records)
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(200, text="<html>not json</html>")))
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(64))
    assert runpod.fetch_gpu_offers().reason == "bad-json"

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(timeout))
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(64))
    assert energy.fetch_elexon().reason == "timeout"


def test_a_failure_is_cached_briefly_and_a_success_for_its_ttl(monkeypatch):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else recorded_handler(request)

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(flaky))
    assert akash.fetch_gpu_offers().reason == "http-500"
    assert akash.fetch_gpu_offers().reason == "http-500" and calls["n"] == 1   # the failure is served from cache
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(64))      # the short TTL has notionally passed
    assert akash.fetch_gpu_offers().ok and calls["n"] == 2
    assert akash.fetch_gpu_offers().ok and calls["n"] == 2                       # the success is cached too


def test_the_date_header_is_the_fallback_observation_time(monkeypatch):
    def no_date(request):
        return httpx.Response(200, json={"models": [{"vendor": "nvidia", "model": "h100", "ram": "80Gi", "interface": "SXM5",
                                                       "availability": {"total": 3, "available": 1}, "price": {"med": 2.5}}]})

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(no_date))
    before = datetime.now(timezone.utc)
    result = akash.fetch_gpu_offers()
    assert result.ok and before <= result.provenance.observed_at <= datetime.now(timezone.utc)
