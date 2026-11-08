"""The power-price history backfill (N-DATA): two years of DE-LU and GB slots loaded lawfully, once, and picked up by the reads.

Every expectation is stated independently of the code: the windows and gaps are worked out by hand, the stored
values are the recorded upstream bodies' own numbers (fixtures under tests/fixtures/recorded/energy/, fetched
2026-11-08 and trimmed to three days and two days without changing shape), and the pressure arithmetic is
computed on paper. No test opens a network connection.
"""

import logging
from datetime import date, datetime, timedelta, timezone

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
from product.flopx import archive, domain, terminal
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import _http, licences
from product.flopx.tools import history_backfill as hb
from product.models import Observation
from product.startup import STARTUP_TASKS, start_history_backfill
from product.flopx.tests.test_flopx_sources_live import RECORDED, _load

UTC = timezone.utc


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(1024))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _record(name: str) -> httpx.Response:
    rec = _load("energy", name)
    return httpx.Response(rec["status"], headers={"Date": rec["date"], "Content-Type": rec["contentType"]}, json=rec["body"])


@pytest.fixture
def recorded(monkeypatch):
    """The two history bodies, served for any window asked of their hosts; the requests are kept for inspection."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.energy-charts.info":
            return _record("energy-charts-de-lu-history")
        if request.url.host == "data.elexon.co.uk":
            return _record("elexon-market-index-history")
        return httpx.Response(404, text="unrouted in test")

    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(hb, "PAUSE_SECONDS", 0.0)
    return seen


DE = "energy:DE-LU:day-ahead"
GB = "energy:GB:market-index"


# -- windows and gaps, by hand ---------------------------------------------------------------------------

def test_the_window_ends_yesterday_and_gaps_are_grouped_into_chunks():
    assert hb.window_for(date(2026, 11, 8), 730) == (date(2024, 11, 8), date(2026, 11, 7))
    assert hb.window_for(date(2026, 11, 8), 1) == (date(2026, 11, 7), date(2026, 11, 7))
    first, last = date(2025, 1, 1), date(2025, 1, 20)
    assert hb.gaps_in(set(), first, last, 7) == [(date(2025, 1, 1), date(2025, 1, 7)), (date(2025, 1, 8), date(2025, 1, 14)), (date(2025, 1, 15), date(2025, 1, 20))]
    held = {date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5)}
    assert hb.gaps_in(held, first, last, 7) == [(date(2025, 1, 1), date(2025, 1, 2)), (date(2025, 1, 6), date(2025, 1, 12)),
                                                (date(2025, 1, 13), date(2025, 1, 19)), (date(2025, 1, 20), date(2025, 1, 20))]
    assert hb.gaps_in({first + timedelta(days=i) for i in range(20)}, first, last, 7) == []
    # The request shapes measured on 2026-11-08: Energy-Charts reads local days (one extra asked for, trimmed by UTC day);
    # Elexon allows seven days From to To inclusive, so a six-day chunk asks for from 00:00 of the first to 00:00 of the day after the last.
    assert hb._energy_charts_params(date(2024, 11, 8), date(2024, 11, 10)) == {"bzn": "DE-LU", "start": "2024-11-08", "end": "2024-11-11"}
    assert hb._elexon_params(date(2025, 1, 1), date(2025, 1, 6)) == {"from": "2025-01-01T00:00Z", "to": "2025-01-07T00:00Z", "dataProviders": "APXMIDP"}
    assert hb.SOURCES[GB].chunk_days == 6 and hb.SOURCES[DE].chunk_days == 60
    assert (RECORDED / "energy" / "energy-charts-de-lu-history.json").exists()


# -- Energy-Charts: load once, resume a gap ------------------------------------------------------------

def test_energy_charts_history_is_loaded_once_with_archived_provenance_and_a_gap_is_refilled(db, recorded):
    report = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=[DE])
    (src,) = report.sources
    assert (src.days_wanted, src.days_held_before, src.gaps, src.requests) == (3, 0, [(date(2024, 11, 8), date(2024, 11, 10))], 1)
    assert dict(recorded[0].url.params) == {"bzn": "DE-LU", "start": "2024-11-08", "end": "2024-11-11"}
    # The recorded body holds 72 hourly slots from 2024-11-07 23:00 UTC: the one slot before the window is trimmed, 71 stored.
    assert (src.inserted, src.updated, src.unchanged, src.days_still_missing, src.failures) == (71, 0, 0, 0, [])
    assert report.complete and not report.all_failed
    stored = archive.archived_series(db, DE)
    assert stored.unit == "EUR/MWh" and len(stored.points) == 71
    assert stored.points[0].t == datetime(2024, 11, 8, 0, 0, tzinfo=UTC) and stored.points[0].value == 99.73      # the body's second price
    assert stored.points[-1].t == datetime(2024, 11, 10, 22, 0, tzinfo=UTC) and stored.points[-1].value == 108.02  # its last
    prov = stored.points[0].provenance
    assert prov.kind == "archived" and prov.source == "energy-charts" and prov.licence == licences.licence("energy-charts") == "CC BY 4.0"
    assert "Bundesnetzagentur | SMARD.de" in prov.note and "loaded by history_backfill" in prov.note
    row = db.query(Observation).filter(Observation.series_id == DE).order_by(Observation.t).first()
    assert row.provenance["backfill"]["window"] == ["2024-11-08", "2024-11-10"] and row.provenance["source"] == "energy-charts"
    assert db.query(Observation).count() == 71
    # A second run finds every day held and asks for nothing:
    again = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=[DE])
    assert (again.sources[0].requests, again.sources[0].days_held_before, again.sources[0].gaps) == (0, 3, []) and len(recorded) == 1
    assert db.query(Observation).count() == 71
    # Deleting one day's rows reopens exactly that gap, and only it is fetched:
    for row in db.query(Observation).filter(Observation.series_id == DE).all():
        if archive._aware(row.t).date() == date(2024, 11, 9):
            db.delete(row)
    db.commit()
    assert db.query(Observation).count() == 47
    third = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=[DE])
    assert third.sources[0].gaps == [(date(2024, 11, 9), date(2024, 11, 9))] and dict(recorded[1].url.params)["start"] == "2024-11-09"
    assert (third.sources[0].inserted, third.sources[0].days_still_missing) == (24, 0) and db.query(Observation).count() == 71
    # A dry run reports the gap and fetches nothing:
    for row in db.query(Observation).filter(Observation.series_id == DE).all():
        db.delete(row)
    db.commit()
    dry = hb.run(db, until=date(2024, 11, 11), days=3, series_ids=[DE], dry_run=True)
    assert dry.dry_run and dry.sources[0].requests == 1 and dry.sources[0].days_still_missing == 3 and len(recorded) == 2
    assert db.query(Observation).count() == 0


# -- Elexon: seven-day chunks, the bound, failures ---------------------------------------------------------

def test_elexon_history_is_loaded_in_six_day_chunks_and_a_run_stops_at_its_bound(db, recorded):
    report = hb.run(db, until=date(2025, 1, 3), days=2, series_ids=[GB])
    (src,) = report.sources
    assert dict(recorded[0].url.params) == {"from": "2025-01-01T00:00Z", "to": "2025-01-03T00:00Z", "dataProviders": "APXMIDP"}
    assert (src.requests, src.inserted, src.days_still_missing) == (1, 96, 0)                 # 2 days x 48 settlement periods
    stored = archive.archived_series(db, GB)
    assert stored.unit == "GBP/MWh"
    assert (stored.points[0].t, stored.points[0].value) == (datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 9.07)      # the body's first row
    assert (stored.points[-1].t, stored.points[-1].value) == (datetime(2025, 1, 2, 23, 30, tzinfo=UTC), 77.61)  # its last
    assert stored.points[0].provenance.source == "elexon-bmrs" and "Contains BMRS data" in stored.points[0].provenance.note
    (row,) = [r for r in hb.status(db, until=date(2025, 1, 3), days=2) if r["series_id"] == GB]
    assert (row["days_held"], row["days_wanted"], row["oldest"], row["newest"]) == (2, 2, date(2025, 1, 1), date(2025, 1, 2))
    # Fourteen days in six-day chunks is three requests; capped at one, the run stops and says so. The recorded body only
    # covers 1 and 2 January, so twelve of the fourteen days stay missing for the next run.
    bounded = hb.run(db, until=date(2025, 1, 15), days=14, series_ids=[GB], max_requests=1)
    (b,) = bounded.sources
    assert b.gaps == [(date(2025, 1, 3), date(2025, 1, 8)), (date(2025, 1, 9), date(2025, 1, 14))]
    assert b.requests == 1 and b.stopped_at_bound and b.days_still_missing == 12 and b.failures == ["empty-response"]


def test_an_upstream_error_is_a_failure_not_an_exception_and_an_unarchivable_source_is_skipped(db, monkeypatch):
    monkeypatch.setattr(_http, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(500, text="down")))
    monkeypatch.setattr(hb, "PAUSE_SECONDS", 0.0)
    report = hb.run(db, until=date(2025, 1, 3), days=2)
    assert [s.failures for s in report.sources] == [["http-500"], ["http-500"]] and report.all_failed and not report.complete
    assert db.query(Observation).count() == 0
    monkeypatch.setattr(licences, "archivable", lambda source: source != "elexon-bmrs")
    skipped = hb.run(db, until=date(2025, 1, 3), days=2)
    assert [s.series_id for s in skipped.sources] == [DE]


def test_the_cli_runs_reports_and_shows_status(db, recorded, monkeypatch, capsys):
    import infrastructure.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    assert hb.main(["run", "--days", "3", "--until", "2024-11-11", "--series", DE, "--dry-run"]) == 0 and db.query(Observation).count() == 0
    assert "would fetch 1 requests" in capsys.readouterr().out
    assert hb.main(["run", "--days", "3", "--until", "2024-11-11", "--series", DE]) == 0 and db.query(Observation).count() == 71
    out = capsys.readouterr().out
    assert "energy-charts: energy:DE-LU:day-ahead 2024-11-08 to 2024-11-10: 0/3 days held before, 1 gaps, fetched 1 requests; 71 inserted, 0 updated, 0 unchanged; 0 days still missing" in out
    assert hb.main(["status", "--days", "3", "--until", "2024-11-11"]) == 0
    out = capsys.readouterr().out
    assert "energy-charts: energy:DE-LU:day-ahead holds 3/3 days of 2024-11-08 to 2024-11-10 (oldest 2024-11-08, newest 2024-11-10)" in out
    assert "elexon-bmrs: energy:GB:market-index holds 0/3 days" in out
    assert hb.main(["run", "--days", "0"]) == 1


# -- the startup seam ---------------------------------------------------------------------------------------

def test_the_startup_task_is_last_refuses_under_pytest_and_loads_on_its_thread_when_forced(db, recorded, caplog):
    assert STARTUP_TASKS[-1] is start_history_backfill
    assert hb.start_background(lambda: db) is False                                            # never the network under pytest
    with caplog.at_level(logging.INFO, logger="product.flopx.tools.history_backfill"):
        started = hb.start_background(lambda: db, force=True, delay=0.0, until=date(2024, 11, 11), days=3, series_ids=[DE])
        assert started is True
        hb._state["thread"].join(timeout=30)
    assert not hb._state["thread"].is_alive()
    assert db.query(Observation).count() == 71
    assert "71 inserted, 0 updated, 0 unchanged; 0 days still missing" in caplog.text
    assert hb.start_background(lambda: db, force=True, delay=0.0, until=date(2024, 11, 11), days=3, series_ids=[DE]) is True   # the first thread ended, so a new one may start
    hb._state["thread"].join(timeout=30)
    assert db.query(Observation).count() == 71                                                 # and found nothing to do


# -- the reads pick the history up without a live answer -----------------------------------------------------

def _archived_power(db, series_id: str, source: str, unit: str, as_of: datetime, *, days: int = 40, level: float = 100.0, spike: float = 150.0) -> int:
    """`days` days of hourly archived slots at `level`, the newest (one second before `as_of`) at `spike`."""
    loaded = utcnow()
    end = as_of - timedelta(seconds=1)
    points = []
    for i in range(days * 24):
        t = end - timedelta(hours=i)
        prov = Provenance(kind="archived", source=source, observed_at=t, fetched_at=loaded, licence=licences.licence(source), note="test history")
        points.append(domain.PriceObservation(series_id, t, spike if i == 0 else level, unit, prov))
    inserted, _, _ = archive.upsert(db, points)
    return inserted


def test_power_stress_the_overview_the_series_and_the_infra_energy_read_the_archived_history(db, monkeypatch):
    monkeypatch.setattr(terminal, "_gather", lambda calls: [])                                 # every live adapter silent
    data = terminal._data()
    as_of = max(o.provenance.observed_at for o in data.offers)                                 # the sample asks fix the grid's as-of
    assert _archived_power(db, DE, "energy-charts", "EUR/MWh", as_of) == 960
    # Pressure: eu-de's grid zone is DE-LU. 40 days of 100 and one slot at 150: the trailing 30-day median is 100,
    # so power-stress raw = 150 / 100 - 1 = 0.5 and normalised = 0.5 / 0.50 = 1.0, on archived Energy-Charts evidence.
    grid = terminal.pressure_grid(db, None, "eu-de")
    cells = grid["cells"]
    assert cells and all(c.region_id == "eu-de" for c in cells)
    factor = next(f for f in cells[0].factors if f.factor == "power-stress")
    assert factor.raw == pytest.approx(0.5) and factor.normalised == pytest.approx(1.0)
    assert [(p.kind, p.source) for p in factor.evidence] == [("archived", "energy-charts")]
    assert "latest 150.00 vs trailing 30-day median 100.00 EUR/MWh" in factor.note
    # A region whose zone has no series still says so (ERCOT: the authored sample series is what it has):
    texas = terminal.pressure_grid(db, None, "us-tx")["cells"][0]
    assert next(f for f in texas.factors if f.factor == "power-stress").evidence[0].kind == "sample"
    # Infra energy: the DE-LU zone row reads the same numbers.
    zone = next(z for z in terminal.infra_energy(db)["zones"] if z.zone == "DE-LU")
    assert (zone.latest, zone.median_30d, zone.unit) == (150.0, 100.0, "EUR/MWh") and zone.provenance.kind == "archived"
    # Overview: with the live source silent the indicator stands on the newest archived slot, labelled archived.
    indicator = next(i for i in terminal.overview(db, None)["indicators"] if i["id"] == DE)
    assert indicator["value"] == 150.0 and indicator["provenance"].kind == "archived" and indicator["provenance"].source == "energy-charts"
    # The series read serves the archived range asked for, nothing live:
    view = terminal.series(db, DE, "7d")
    assert view["points"] and {p.provenance.kind for p in view["points"]} == {"archived"}
    assert view["points"][-1].value == 150.0 and view["points"][0].t >= view["points"][-1].t - timedelta(days=7)
    assert view["freshness"].sources == ("energy-charts",)


def test_without_history_the_power_stress_factor_has_no_data_as_before(db, monkeypatch):
    monkeypatch.setattr(terminal, "_gather", lambda calls: [])
    cell = terminal.pressure_grid(db, None, "eu-de")["cells"][0]
    factor = next(f for f in cell.factors if f.factor == "power-stress")
    assert factor.normalised is None and factor.evidence == [] and "no day-ahead energy series for grid zone DE-LU" in factor.note
    assert DE not in {i["id"] for i in terminal.overview(db, None)["indicators"]}
