"""EIA API v2 (Form EIA-930): dormant without the key, live ERCOT and PJM hourly demand with it (N-EIA).

The recorded bodies under tests/fixtures/recorded/eia/ were fetched once on 2026-11-08 at 10:47Z
(the demo key, redacted from the recorded URL) and are served through `httpx.MockTransport`; no
test reaches the network and no real key is ever present. Expectations are stated from the
bodies by hand: ERCO had 35 demand hours (2026-11-07T00 to 2026-11-08T10) and 31 forecast
hours (to 06h); PJM had 35 demand hours and 45 forecast hours running to 2026-11-08T23, of which
13 (11h to 23h) had not started at the fetch; the three-day history slice holds 72 demand and
72 forecast hours.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.exceptions import UnknownSeriesError
from product.flopx import terminal
from product.flopx.sources import _http, eia, licences
from product.flopx.tools import history_backfill as hb
from product.models import Observation

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "eia"
RECORDED_AT = datetime(2026, 11, 8, 10, 47, 56, tzinfo=timezone.utc)
DUMMY_KEY = "test-only-eia-key-0000"
UTC = timezone.utc


def _rec(name: str) -> dict:
    return json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))


def _response(name: str) -> httpx.Response:
    rec = _rec(name)
    return httpx.Response(rec["status"], headers={"Date": rec["date"], "Content-Type": rec["contentType"]}, json=rec["body"])


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    _http.reset_jobs()
    monkeypatch.setitem(_http.MIN_INTERVAL_SECONDS, "api.eia.gov", 0.0)
    yield
    _http.reset_jobs()


@pytest.fixture
def recorded(monkeypatch):
    """Routes api.eia.gov by respondent and window; every other host is unrouted. Requests are kept."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "api.eia.gov":
            return httpx.Response(404, text="unrouted in test")
        q = dict(request.url.params)
        if q.get("api_key") != DUMMY_KEY:
            return httpx.Response(403, json={"error": {"code": "API_KEY_INVALID", "message": "invalid"}})
        if q.get("start", "").startswith("2024-"):
            return _response("region-data-erco-history")
        return _response("region-data-pjm" if q.get("facets[respondent][0]") == "PJM" else "region-data-erco")

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(handler))
    return seen


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(eia.s, "EIA_API_KEY", DUMMY_KEY)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


# -- without the key: dormant and invisible -----------------------------------------------------------------

def test_without_the_key_nothing_is_asked_and_the_zones_read_as_before(recorded, db, caplog):
    assert eia.s.EIA_API_KEY in (None, "") and eia.configured() is False
    result = eia.fetch_zone("ERCOT")
    assert result.ok is False and result.unavailable == {"source": "eia", "reason": "not-configured"}
    assert recorded == []                                                      # no request, not even a refused one
    assert len(terminal._energy_calls()) == 2                                  # Energy-Charts and Elexon only
    assert set(terminal.live_series()) == {"energy:DE-LU:day-ahead", "energy:GB:market-index"}
    with pytest.raises(UnknownSeriesError):
        terminal.series(db, "energy:ERCOT:demand", None)
    # The backfill skips the four rows and says so; status does not list them.
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.history_backfill"):
        report = hb.run(db, until=date(2024, 11, 11), days=3, dry_run=True)
    assert {r.source for r in report.sources} == {"energy-charts", "elexon-bmrs"}
    assert "eia: energy:ERCOT:demand not configured (no key); history not loaded" in caplog.text
    assert {row["source"] for row in hb.status(db, until=date(2024, 11, 11), days=3)} == {"energy-charts", "elexon-bmrs"}
    assert recorded == []


# -- the licence row ---------------------------------------------------------------------------------------

def test_the_licence_row_is_public_domain_and_archivable_in_both_copies():
    row = licences.policy("eia")
    assert row["licence"] == "US public domain (EIA)" and row["use"] == "display-and-archive" and row["asserted"] is False
    assert licences.licence("eia") == "US public domain (EIA)"                  # measured, so no asserted caveat
    assert licences.archivable("eia") is True and licences.in_index("eia") is True and licences.public("eia") is True
    frontend = Path(__file__).resolve().parents[4] / "next-pwa" / "product" / "features" / "flopx" / "data" / "licences.json"
    assert frontend.read_bytes() == licences.TABLE_PATH.read_bytes()


# -- parsing, by hand --------------------------------------------------------------------------------------

def test_parse_keeps_only_hours_already_started_and_names_the_series_by_type():
    pjm = eia.parse(_rec("region-data-pjm")["body"], "PJM", RECORDED_AT)
    assert len(pjm) == 67                                                      # 35 demand + 32 forecast hours to 10h; 13 future forecasts dropped
    assert {p.series_id for p in pjm} == {"energy:PJM:demand", "energy:PJM:demand-forecast"}
    assert max(p.t for p in pjm) == datetime(2026, 11, 8, 10, tzinfo=UTC)
    first_d = next(p for p in pjm if p.series_id == "energy:PJM:demand")
    first_df = next(p for p in pjm if p.series_id == "energy:PJM:demand-forecast")
    assert (first_d.t, first_d.value, first_d.unit) == (datetime(2026, 11, 7, 0, tzinfo=UTC), 93975.0, "MWh")
    assert (first_df.t, first_df.value) == (datetime(2026, 11, 7, 0, tzinfo=UTC), 91339.0)
    assert first_d.provenance.kind == "live" and first_d.provenance.source == "eia" and first_d.provenance.observed_at == first_d.t
    assert first_d.provenance.licence == "US public domain (EIA)"
    assert "hourly demand for PJM (PJM Interconnection, LLC)" in first_d.provenance.note and "Form EIA-930" in first_d.provenance.note
    assert "day-ahead demand forecast" in first_df.provenance.note
    # A PJM body parsed for ERCOT yields nothing: rows are matched on the respondent, never trusted by position.
    assert eia.parse(_rec("region-data-pjm")["body"], "ERCOT", RECORDED_AT) == []
    assert eia.period("2026-11-08T09") == datetime(2026, 11, 8, 9, tzinfo=UTC) and eia.period("2026-11-08") is None
    assert eia.zone_of("energy:ERCOT:demand-forecast") == ("ERCOT", "DF")


# -- with the key: live series, the key never written anywhere -------------------------------------------------

def test_with_the_key_both_zones_are_live_and_the_key_rides_only_in_the_query(recorded, keyed, db, caplog):
    assert eia.configured() is True
    ercot = eia.fetch_zone("ERCOT")
    assert ercot.ok, ercot.reason
    assert {p.series_id for p in ercot.records} == {"energy:ERCOT:demand", "energy:ERCOT:demand-forecast"}
    assert all(p.unit == "MWh" and p.provenance.kind == "live" and p.provenance.fetched_at is not None for p in ercot.records)
    assert ercot.provenance.licence == "US public domain (EIA)" and ercot.provenance.source == "eia"
    demand = [p for p in ercot.records if p.series_id == "energy:ERCOT:demand"]
    assert demand[0].value == 55860.0 and len(demand) == 35
    # One request, the key as the api_key query parameter and nowhere else:
    assert len(recorded) == 1
    q = dict(recorded[0].url.params)
    assert q["api_key"] == DUMMY_KEY and q["facets[respondent][0]"] == "ERCO" and {q["facets[type][0]"], q["facets[type][1]"]} == {"D", "DF"}
    assert q["frequency"] == "hourly" and q["length"] == "5000"
    for p in ercot.records:
        assert DUMMY_KEY not in (p.provenance.note or "") and DUMMY_KEY not in (p.provenance.source_url or "")
    assert not any(DUMMY_KEY in key for key in _http._jobs)                    # the cache key is zone and window
    # The terminal: four calls, four live-only series, the series read answers in MWh naming the source.
    assert len(terminal._energy_calls()) == 4
    assert set(eia.SERIES_IDS) <= set(terminal.live_series())
    body = terminal.series(db, "energy:PJM:demand", None)
    assert body["unit"] == "MWh" and body["points"] and all(p.series_id == "energy:PJM:demand" for p in body["points"])
    assert "eia" in body["freshness"].sources
    assert db.query(Observation).filter(Observation.series_id == "energy:PJM:demand").count() > 0   # archived on the read
    # A refused key is an unavailable result, and the log line carries the host and path, never the key.
    # (The cache key is zone and window, so the held answer is dropped first: a key swap mid-process is not a
    # case the cache distinguishes, by design.)
    monkeypatch_key = "another-wrong-key-1111"
    eia.s.EIA_API_KEY = monkeypatch_key
    _http.reset_jobs()
    cache_module.cache._backend = MemoryBackend(2048)
    with caplog.at_level(logging.WARNING, logger="product.flopx.sources._http"):
        refused = eia.fetch_zone("PJM")
    assert refused.ok is False and refused.reason == "http-403"
    assert "api.eia.gov/v2/electricity/rto/region-data/data/" in caplog.text and monkeypatch_key not in caplog.text


# -- the history backfill ------------------------------------------------------------------------------------

def test_the_backfill_loads_one_type_per_request_in_200_day_chunks_with_archived_provenance(recorded, keyed, db, monkeypatch):
    monkeypatch.setattr(hb, "PAUSE_SECONDS", 0.0)
    assert {sid for sid, src in hb.SOURCES.items() if src.source == "eia"} == set(eia.SERIES_IDS)
    src = hb.SOURCES["energy:ERCOT:demand"]
    assert src.chunk_days == 200 and src.chunk_days * 24 <= 5000 and src.enabled() is True
    params = eia.history_params("energy:PJM:demand-forecast", date(2024, 1, 1), date(2024, 1, 3))
    assert params["start"] == "2024-01-01T00" and params["end"] == "2024-01-03T23" and params["facets[type][0]"] == "DF"
    assert "facets[type][1]" not in params and params["facets[respondent][0]"] == "PJM" and params["api_key"] == DUMMY_KEY

    report = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=["energy:ERCOT:demand"])
    [line] = report.lines()
    assert line.startswith("eia: energy:ERCOT:demand 2024-11-08 to 2024-11-10: 0/3 days held before, 1 gaps, fetched 1 requests; 72 inserted")
    assert report.complete and len(recorded) == 1
    assert dict(recorded[0].url.params)["facets[type][0]"] == "D" and "facets[type][1]" not in dict(recorded[0].url.params)
    rows = db.query(Observation).filter(Observation.series_id == "energy:ERCOT:demand").order_by(Observation.t).all()
    assert len(rows) == 72 and rows[0].value == 55515.0
    prov = rows[0].provenance
    assert prov["kind"] == "archived" and prov["source"] == "eia" and prov["licence"] == "US public domain (EIA)"
    assert "loaded by history_backfill" in prov["note"] and prov["backfill"]["window"] == ["2024-11-08", "2024-11-10"]
    assert DUMMY_KEY not in json.dumps(prov)
    # The forecast series is its own row and still empty; status says so.
    held = {row["series_id"]: row["days_held"] for row in hb.status(db, until=date(2024, 11, 11), days=3)}
    assert held["energy:ERCOT:demand"] == 3 and held["energy:ERCOT:demand-forecast"] == 0
    # A rerun finds nothing missing and asks for nothing.
    again = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=["energy:ERCOT:demand"])
    assert again.sources[0].requests == 0 and len(recorded) == 1
