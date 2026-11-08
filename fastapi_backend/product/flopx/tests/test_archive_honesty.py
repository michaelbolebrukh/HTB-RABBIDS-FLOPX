"""What the archive may serve, and what an index may compare (re-walk R1, findings R1-4, R1-5, R1-9, R1-11, R1-12).

The archive keeps every row it ever stored; these tests pin what it hands back. Rows are seeded the way
the running stack holds them: Vast.ai asks archived while Vast was still polled (licence string and kind
`live` on the stored envelope), FCPI points archived before the basket was recorded, and day-ahead power
slots archived before their delivery time. Live sources are replaced at their module seam with the
recorded upstream bodies parsed by the real adapters (RunPod and Akash publish region `global`; Vast.ai,
a market reading under decision G9-b, is stubbed unavailable here and covered by test_vast_market_reading.py),
never the network.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.exceptions import InsufficientHistoryError, UnknownSeriesError
from product.flopx import archive, domain, terminal
from product.flopx.provenance import live
from product.flopx.sources import akash, available, energy, kalshi, news, polymarket, runpod, sec, unavailable, vast, wikidata, yahoo
from product.models import Observation

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
ENGINE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "engine"
FETCHED = datetime(2026, 11, 7, 15, 51, 9, tzinfo=timezone.utc)      # when the recorded bodies were fetched
NOW = datetime(2026, 11, 7, 21, 13, tzinfo=timezone.utc)             # the re-walk's clock
H100 = "h100-sxm-80gb"
VAST_LICENCE = "Vast.ai public search API; display with attribution"


def _body(path: str) -> dict:
    return json.loads((RECORDED / path).read_text(encoding="utf-8"))["body"]


RUNPOD_ROWS = runpod.parse(_body("runpod/gpu-types.json"), FETCHED, FETCHED)
AKASH_ROWS = akash.parse(_body("akash/gpu-prices.json"), FETCHED, FETCHED)
DE_LU_ROWS = energy.parse_energy_charts(_body("energy/energy-charts-de-lu.json"), "DE-LU", FETCHED)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(archive, "utcnow", lambda: NOW)
    monkeypatch.setattr(terminal, "utcnow", lambda: NOW)


def _sources(monkeypatch, *, runpod_rows=RUNPOD_ROWS, akash_rows=AKASH_ROWS, energy_rows=DE_LU_ROWS, facilities=None):
    """Every adapter at its seam: RunPod, Akash and Energy-Charts from the recorded bodies, the rest unavailable
    (Vast.ai's market reading is exercised in test_vast_market_reading.py)."""
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(4096))
    monkeypatch.setattr(vast, "fetch_gpu_offers", lambda: unavailable("vast-ai", "timeout"))
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", runpod_rows, live("runpod", FETCHED, fetched_at=FETCHED, licence="x")) if runpod_rows is not None else unavailable("runpod", "timeout"))
    monkeypatch.setattr(akash, "fetch_gpu_offers", lambda: available("akash", akash_rows, live("akash", FETCHED, fetched_at=FETCHED, licence="x")) if akash_rows is not None else unavailable("akash", "timeout"))
    monkeypatch.setattr(energy, "fetch_energy_charts", lambda zone="DE-LU", days=1: available("energy-charts", energy_rows, energy_rows[-1].provenance) if energy_rows else unavailable("energy-charts", "timeout"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: unavailable("elexon-bmrs", "timeout"))
    monkeypatch.setattr(wikidata, "fetch_facilities", lambda: available("wikidata", facilities, live("wikidata", FETCHED, licence="CC0")) if facilities else unavailable("wikidata", "timeout"))
    monkeypatch.setattr(polymarket, "fetch_prediction_markets", lambda: unavailable("polymarket-gamma", "timeout"))
    monkeypatch.setattr(kalshi, "fetch_series", lambda series: unavailable(f"kalshi:{series}", "timeout"))
    monkeypatch.setattr(news, "fetch_hn", lambda query=None, limit=20: unavailable("hacker-news", "timeout"))
    monkeypatch.setattr(news, "fetch_rss", lambda key, limit=10: unavailable(f"rss:{key}", "timeout"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(yahoo, "fetch_latest_close", lambda ticker: None)


def _seed(db, series_id: str, t: datetime, value: float, provenance: dict, unit: str = "USD/GPU-hour") -> None:
    """A row written straight into the table, the way the stack holds rows archived before these fixes."""
    db.add(Observation(series_id=series_id, t=t, value=value, unit=unit, provenance=provenance))
    db.commit()


def _vast_envelope(t: datetime) -> dict:
    return {"kind": "live", "source": "vast-ai", "observed_at": t.isoformat(), "fetched_at": t.isoformat(), "licence": VAST_LICENCE, "input_kinds": []}


def _index_envelope(t: datetime, basket: list[str] | None) -> dict:
    env = {"kind": "derived", "source": "fcpi-v1:median-of-adjusted-asks", "observed_at": t.isoformat(), "fetched_at": t.isoformat(), "input_kinds": ["live"]}
    if basket is not None:
        env["input_sources"] = basket
    return env


# -- R1-5: a source that is off never leaves the archive --------------------------------------------

def test_archived_vast_offer_series_are_refused_at_read_and_kept_in_the_table(db, clock):
    for minutes in (0, 30, 60):
        t = datetime(2026, 11, 7, 19, 18, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        _seed(db, "offer:vast-25518950:available", t, 1.0, _vast_envelope(t), unit="share")
        _seed(db, "offer:vast-25518950:price", t, 2.4, _vast_envelope(t))
    with pytest.raises(UnknownSeriesError):
        terminal.series(db, "offer:vast-25518950:available", None)
    assert archive.archived_series(db, "offer:vast-25518950:price") is None
    assert archive.latest_points(db, ["offer:vast-25518950:price"], NOW) == {}
    assert archive.latest_before(db, "offer:vast-25518950:price", NOW) is None
    assert db.query(Observation).count() == 6                         # refused, not deleted


def test_a_vast_row_is_never_written_and_an_index_point_without_its_basket_is_never_written(db, clock):
    t = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
    vast_ask = domain.PriceObservation("offer:vast-1:price", t, 2.4, "USD/GPU-hour", live("vast-ai", t, fetched_at=t, licence=VAST_LICENCE))
    index_point = domain.PriceObservation(f"index:fcpi:{H100}", t, 2.1, "USD/GPU-hour", live("runpod", t, fetched_at=t))
    assert archive.record(db, [vast_ask, index_point]) == 0
    assert archive.record(db, [index_point], baskets={index_point.series_id: ["runpod", "akash"]}) == 1
    stored = db.query(Observation).one()
    assert stored.provenance["input_sources"] == ["akash", "runpod"]


def test_fcpi_points_built_with_vast_or_with_no_recorded_basket_are_not_published_as_history(db, clock, monkeypatch):
    _sources(monkeypatch)
    sid = f"index:fcpi:{H100}"
    legacy = [datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * k) for k in range(3)]
    for k, t in enumerate(legacy):
        _seed(db, sid, t, 2.70 + k / 100, _index_envelope(t, None))                       # archived before baskets were recorded
    with_vast = datetime(2026, 11, 7, 19, 40, tzinfo=timezone.utc)
    _seed(db, sid, with_vast, 2.55, _index_envelope(with_vast, ["akash", "runpod", "vast-ai"]))
    kept = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    _seed(db, sid, kept, 2.31, _index_envelope(kept, ["akash", "runpod"]))

    payload = terminal.series(db, sid, None)
    values = [p.value for p in payload["points"]]
    assert 2.70 not in values and 2.71 not in values and 2.72 not in values and 2.55 not in values
    assert values[0] == 2.31                                                              # the one point with a lawful basket
    ix = terminal._index_for(db, terminal._data(), H100, [*RUNPOD_ROWS, *AKASH_ROWS])
    assert any("4 earlier archived points withheld" in line for line in ix.limitations)
    assert db.query(Observation).filter(Observation.series_id == sid).count() == 5       # nothing deleted


# -- R1-12: a day-ahead slot archived before its delivery time is not the latest price -----------------

def test_future_dated_archived_power_slots_are_refused_and_the_latest_point_is_the_live_one(db, clock, monkeypatch):
    sid = "energy:DE-LU:day-ahead"
    written = datetime(2026, 11, 7, 19, 34, tzinfo=timezone.utc)
    for k in range(7):                                                                    # 19:45 to 22:45, archived at 19:34
        t = datetime(2026, 11, 7, 19, 45, tzinfo=timezone.utc) + timedelta(minutes=30 * k)
        _seed(db, sid, t, 224.7, {"kind": "live", "source": "energy-charts", "observed_at": t.isoformat(), "fetched_at": written.isoformat(),
                                  "licence": "CC BY 4.0", "input_kinds": []}, unit="EUR/MWh")
    live_now = [domain.PriceObservation(sid, NOW.replace(minute=0), 277.13, "EUR/MWh", live("energy-charts", NOW.replace(minute=0), fetched_at=NOW, licence="CC BY 4.0"))]
    _sources(monkeypatch, energy_rows=live_now)
    payload = terminal.series(db, sid, None)
    assert max(p.t for p in payload["points"]) <= NOW
    assert payload["points"][-1].value == 277.13
    assert payload["points"][-1].provenance.kind == "live"
    future = domain.PriceObservation(sid, NOW + timedelta(hours=1), 300.0, "EUR/MWh", live("energy-charts", NOW + timedelta(hours=1), fetched_at=NOW, licence="CC BY 4.0"))
    assert archive.record(db, [future]) == 0                                              # refused on write as well


# -- R1-4: a basket change is a series break, and no reading spans it ----------------------------------

def test_a_basket_change_is_marked_and_change_and_forecast_read_only_since_it(db, clock, monkeypatch):
    sid = f"index:fcpi:{H100}"
    for k in range(6):                                                                    # six daily points from RunPod and Akash, from ten days back
        t = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc) + timedelta(days=k)
        _seed(db, sid, t, 1.20 + k / 100, _index_envelope(t, ["akash", "runpod"]))
    first_runpod_only = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
    for k in range(7):                                                                    # then seven from RunPod alone
        t = first_runpod_only + timedelta(minutes=15 * k)
        _seed(db, sid, t, 1.50 + k / 100, _index_envelope(t, ["runpod"]))
    _sources(monkeypatch, akash_rows=None)                                               # Akash stops answering: RunPod alone now
    rows = [replace(o, provenance=replace(o.provenance, observed_at=NOW)) for o in RUNPOD_ROWS]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", rows, live("runpod", NOW, fetched_at=NOW, licence="x")))

    ix = terminal._index_for(db, terminal._data(), H100, rows)
    assert ix.basket == ["runpod"]
    assert len(ix.breaks) == 1
    brk = ix.breaks[0]
    assert (brk.t, brk.reason, brk.before, brk.after) == (first_runpod_only, "basket-changed", ["akash", "runpod"], ["runpod"])
    assert any(line.startswith("basket changed on 2026-11-07") for line in ix.limitations)

    payload = terminal.series(db, sid, None)
    assert len(payload["points"]) == 14 and [b.t for b in payload["breaks"]] == [first_runpod_only]   # the history is still shown, with the break on it
    fc = terminal.forecast(db, sid, 1, 0.8)
    assert fc["history_break"].t == first_runpod_only
    assert fc["history_points"] == 8                                                     # seven RunPod-only points and the current one
    indicator = next(i for i in terminal.overview(db, None)["indicators"] if i["id"] == sid)
    assert indicator["change_pct"] is None        # a 7-day change would reach back across the break to a point in the old basket


def test_a_forecast_with_no_history_since_the_break_refuses_rather_than_spanning_it(db, clock, monkeypatch):
    sid = f"index:fcpi:{H100}"
    for k in range(6):                                                                    # daily, from ten days back
        t = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc) + timedelta(days=k)
        _seed(db, sid, t, 1.20 + k / 100, _index_envelope(t, ["akash", "runpod"]))
    _sources(monkeypatch, akash_rows=None)
    rows = [replace(o, provenance=replace(o.provenance, observed_at=NOW)) for o in RUNPOD_ROWS]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", rows, live("runpod", NOW, fetched_at=NOW, licence="x")))
    with pytest.raises(InsufficientHistoryError):
        terminal.forecast(db, sid, 5, 0.8)
    indicator = next(i for i in terminal.overview(db, None)["indicators"] if i["id"] == sid)
    assert indicator["change_pct"] is None


def test_an_unchanged_basket_marks_no_break(db, clock, monkeypatch):
    sid = f"index:fcpi:{H100}"
    for k in range(3):                                                                    # before the recorded fetch at 15:51
        t = datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * k)
        _seed(db, sid, t, 1.2, _index_envelope(t, ["akash", "runpod"]))
    _sources(monkeypatch)
    ix = terminal._index_for(db, terminal._data(), H100, [*RUNPOD_ROWS, *AKASH_ROWS])   # both recorded bodies carry H100 asks
    assert ix.basket == ["akash", "runpod"]
    assert ix.breaks == [] and len(ix.values) == 4


# -- R1-11: the energy layer shows the live series the overview shows ----------------------------------

def test_region_energy_indicator_is_the_live_series_where_it_answered_and_the_sample_elsewhere(monkeypatch):
    _sources(monkeypatch)
    rows = {r["region"].id: r["energy_indicator"] for r in terminal.regions()["regions"]}
    assert rows["eu-de"] is not None and rows["eu-de"].series_id == "energy:DE-LU:day-ahead"
    assert rows["eu-de"].provenance.kind == "live" and rows["eu-de"].value == DE_LU_ROWS[-1].value
    assert rows["us-tx"].series_id == "energy:ERCOT:day-ahead" and rows["us-tx"].provenance.kind == "sample"
    assert rows["us-va"] is None

    _sources(monkeypatch, energy_rows=[])
    payload = terminal.regions()
    rows = {r["region"].id: r["energy_indicator"] for r in payload["regions"]}
    assert rows["eu-de"] is None                                                          # absent, never filled from sample
    assert any(u["source"] == "energy-charts" for u in payload["freshness"].unavailable)


# -- R1-9: one evaluation whichever tier answers, archive or not -----------------------------------------

def _evaluation_payload(db, tickers):
    from product.flopx.tests.test_engine_fixtures import exporter

    rows = []
    for t in tickers:
        out = exporter.to_json(terminal._evaluate_ticker_uncached(db, t)["evaluation"])
        out.pop("provenance", None)
        rows.append(out)
    return json.loads(exporter.render([{"x": {"evaluations": rows}}]))[0]["x"]


@pytest.mark.parametrize("regional", [False, True], ids=["live-asks-global", "a-regional-live-h100-ask"])
def test_evaluation_with_an_archive_and_live_sources_equals_the_route_handler_tier(db, clock, monkeypatch, regional):
    """The route-handler tier evaluates the sample bag (engine-fixtures.test.ts pins it to evaluation_terminal.json);
    the backend, with live asks, live facilities and an archive holding offer and index history, must equal it.
    The regional case reproduces the stack before Vast went off (a live H100 ask placed in a region), the shape
    that moved 15 of 32 skills and flipped NBIS 21d in the re-walk."""
    asks = list(RUNPOD_ROWS)
    if regional:
        asks += [replace(o, id=o.id + "-va", region_id="us-va") for o in RUNPOD_ROWS if o.gpu_model_id == H100]
    facilities = wikidata.parse(_body("wikidata/sparql-data-centres.json"), FETCHED, FETCHED)
    _sources(monkeypatch, runpod_rows=asks, facilities=facilities)
    for k in range(4):
        t = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc) + timedelta(hours=k)
        for o in asks:
            _seed(db, f"offer:{o.id}:price", t, o.price_per_gpu_hour_usd * (1 + k / 50), {"kind": "live", "source": "runpod", "observed_at": t.isoformat(), "fetched_at": t.isoformat(), "input_kinds": []})
        _seed(db, f"index:fcpi:{H100}", t, 2.0 + k / 10, _index_envelope(t, ["akash", "runpod"]))
    expected = json.loads((ENGINE_FIXTURES / "evaluation_terminal.json").read_text(encoding="utf-8"))
    case = next(c for c in expected if c["name"] == "sample-bag-crwv-and-nvda")
    assert _evaluation_payload(db, case["input"]["tickers"]) == case["expected"]
