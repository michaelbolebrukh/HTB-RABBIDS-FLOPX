"""Vast.ai as a current market reading, never an index (founder decision G9-b, 2026-11-07).

The licence row is `display-not-index`: Vast asks are called again, shown in the offers table and fed
to the regional pressure grid (they carry regions, so a model's cells can be live again), but they
never enter an FCPI basket, a heatmap median or the overview snapshot median, and nothing Vast is
written to the archive. Every Vast row says "Vast.ai asks: a current market reading, not an index or
benchmark" in its provenance note, which is how a payload carrying Vast rows carries the line.

Stubs are the realistic shape (run diagnostic F6): every marketplace answers with its recorded
upstream body parsed by the real adapter. RunPod and Akash publish region `global`; Vast publishes a
host location, which places the recorded H100 SXM asks in Japan (`ap-jp`, a table region) and India,
and two RTX 4090 asks in Texas (`us-tx`). Expectations are written out, never read back from the code
under test.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.flopx import archive, domain, terminal
from product.flopx.provenance import effective_kind, live
from product.flopx.sources import akash, available, energy, kalshi, licences, news, polymarket, runpod, sec, unavailable, vast, wikidata, yahoo
from product.models import Observation

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
FETCHED = datetime(2026, 11, 7, 15, 51, 9, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 7, 15, 52, tzinfo=timezone.utc)
H100 = "h100-sxm-80gb"
LINE = "Vast.ai asks: a current market reading, not an index or benchmark"


def _body(path: str) -> dict:
    return json.loads((RECORDED / path).read_text(encoding="utf-8"))["body"]


VAST_ROWS = vast.parse(_body("vast/bundles.json"), FETCHED, FETCHED)
RUNPOD_ROWS = runpod.parse(_body("runpod/gpu-types.json"), FETCHED, FETCHED)
AKASH_ROWS = akash.parse(_body("akash/gpu-prices.json"), FETCHED, FETCHED)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _sources(monkeypatch, *, with_vast: bool = True, with_others: bool = True) -> None:
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(4096))
    monkeypatch.setattr(archive, "utcnow", lambda: NOW)
    monkeypatch.setattr(terminal, "utcnow", lambda: NOW)
    monkeypatch.setattr(vast, "fetch_gpu_offers", (lambda: available("vast-ai", VAST_ROWS, live("vast-ai", FETCHED, fetched_at=FETCHED, licence=vast.LICENCE, note=f"{LINE}. recorded")))
                        if with_vast else (lambda: unavailable("vast-ai", "timeout")))
    monkeypatch.setattr(runpod, "fetch_gpu_offers", (lambda: available("runpod", RUNPOD_ROWS, live("runpod", FETCHED, fetched_at=FETCHED, licence=runpod.LICENCE)))
                        if with_others else (lambda: unavailable("runpod", "timeout")))
    monkeypatch.setattr(akash, "fetch_gpu_offers", (lambda: available("akash", AKASH_ROWS, live("akash", FETCHED, fetched_at=FETCHED, licence=akash.LICENCE)))
                        if with_others else (lambda: unavailable("akash", "timeout")))
    monkeypatch.setattr(energy, "fetch_energy_charts", lambda zone="DE-LU", days=1: unavailable("energy-charts", "timeout"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: unavailable("elexon-bmrs", "timeout"))
    monkeypatch.setattr(wikidata, "fetch_facilities", lambda: unavailable("wikidata", "timeout"))
    monkeypatch.setattr(polymarket, "fetch_prediction_markets", lambda: unavailable("polymarket-gamma", "timeout"))
    monkeypatch.setattr(kalshi, "fetch_series", lambda series: unavailable(f"kalshi:{series}", "timeout"))
    monkeypatch.setattr(news, "fetch_hn", lambda query=None, limit=20: unavailable("hacker-news", "timeout"))
    monkeypatch.setattr(news, "fetch_rss", lambda key, limit=10: unavailable(f"rss:{key}", "timeout"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "timeout"))
    monkeypatch.setattr(yahoo, "fetch_latest_close", lambda ticker: None)


# -- the recorded body, as the adapter parses it --------------------------------------------------

def test_the_recorded_vast_asks_carry_regions_and_say_what_they_are():
    placed = {(o.id, o.gpu_model_id, o.region_id) for o in VAST_ROWS}
    assert ("vast-59433335", H100, "ap-jp") in placed and ("vast-54322834", H100, "in") in placed
    assert ("vast-55934598", "rtx-4090-24gb", "us-tx") in placed
    assert all(o.provenance.note.startswith(f"{LINE}. ") for o in VAST_ROWS)
    assert all(o.provenance.licence.startswith("Vast.ai public search API, shown as a current market reading only") for o in VAST_ROWS)


# -- shown: the offers table ------------------------------------------------------------------------

def test_vast_asks_appear_in_the_offers_table_each_with_the_market_reading_line(db, monkeypatch):
    _sources(monkeypatch)
    body = terminal.offers(db, H100, None, None, None, "quoted")
    vast_rows = [o for o in body["offers"] if o["provider_id"] == "vast-ai"]
    assert {o["id"] for o in vast_rows} == {"vast-59433335", "vast-54322834"}
    assert {o["price_per_gpu_hour_usd"] for o in vast_rows} == {2.3348, 1.9342}
    assert all(o["provenance"].note.startswith(LINE) and o["provenance"].kind == "live" for o in vast_rows)
    assert "vast-ai" in body["freshness"].sources


# -- fed: the regional pressure grid ------------------------------------------------------------------

def _grid_cell(db, region: str, gpu: str) -> domain.PressureCell:
    return next(c for c in terminal.pressure_grid(db, gpu, region)["cells"] if c.region_id == region)


def test_vast_asks_make_the_h100_japan_cell_live_and_its_evidence_carries_the_line(db, monkeypatch):
    _sources(monkeypatch)
    cell = _grid_cell(db, "ap-jp", H100)
    premium = next(f for f in cell.factors if f.factor == "price-premium")
    assert [p.source for p in premium.evidence] == ["vast-ai"]                       # the one recorded Japan ask
    assert premium.evidence[0].note.startswith(LINE)
    assert premium.evidence[0].kind == "live" and "live" in cell.input_kinds      # other factors (facilities) may still be sample
    detail = terminal.pressure_cell(db, "ap-jp", H100)
    assert any(p.source == "vast-ai" and p.note.startswith(LINE) for f in detail["factors"] for p in f.evidence)
    assert "vast-ai" in detail["freshness"].sources


def test_without_vast_the_same_cell_falls_back_to_sample_asks(db, monkeypatch):
    # RunPod and Akash publish no region, so they cannot populate a cell: this is what Vast restores.
    _sources(monkeypatch, with_vast=False)
    cell = _grid_cell(db, "ap-jp", H100)
    premium = next(f for f in cell.factors if f.factor == "price-premium")
    assert all(effective_kind(p) == "sample" for p in premium.evidence)


# -- never an index ---------------------------------------------------------------------------------------

def test_no_fcpi_basket_contains_vast_and_the_index_level_is_the_one_without_it(db, monkeypatch):
    _sources(monkeypatch)
    with_vast = {ix.gpu_model_id: ix for ix in terminal.indices(None)["indices"]}
    assert all("vast-ai" not in (ix.basket or []) for ix in with_vast.values())
    assert with_vast[H100].basket == ["akash", "runpod"]
    _sources(monkeypatch, with_vast=False)
    without = {ix.gpu_model_id: ix for ix in terminal.indices(None)["indices"]}
    for gid, ix in with_vast.items():
        assert ix.current == without[gid].current and ix.coverage.offer_count == without[gid].coverage.offer_count


def test_vast_alone_builds_no_index_heatmap_or_snapshot_median(db, monkeypatch):
    _sources(monkeypatch, with_others=False)
    assert all(effective_kind(ix.provenance) == "sample" for ix in terminal.indices(db)["indices"])
    single = terminal.index(db, H100)
    assert effective_kind(single["provenance"]) == "sample" and "vast-ai" not in single["freshness"].sources
    heat = terminal.heatmap(db, "median-raw-price", "region")
    assert heat["legend"]["liveCells"] == 0 and "vast-ai" not in heat["freshness"].sources
    snapshot = {row["gpu_model_id"]: row for row in terminal.overview(db, None)["gpu_snapshot"]}
    assert all(row["provenance"].input_kinds == ("sample",) for row in snapshot.values())
    # The pressure grid still reads the market: the Japan H100 cell is live on Vast alone,
    # while the momentum factor keeps the sample series (no live index exists to read).
    cell = _grid_cell(db, "ap-jp", H100)
    assert [p.source for f in cell.factors if f.factor == "price-premium" for p in f.evidence] == ["vast-ai"]
    momentum = next(f for f in cell.factors if f.factor == "price-momentum")
    assert all(effective_kind(p) == "sample" for p in momentum.evidence)


# -- never archived ---------------------------------------------------------------------------------------

def test_nothing_vast_is_archived_across_every_read_that_fetches_asks(db, monkeypatch):
    _sources(monkeypatch)
    terminal.offers(db, None, None, None, None, None)
    terminal.indices(db)
    terminal.index(db, H100)
    terminal.series(db, f"index:fcpi:{H100}", None)
    terminal.heatmap(db, "median-raw-price", "region")
    terminal.changes(db, None)
    terminal.overview(db, None)
    terminal.pressure_grid(db, None, None)
    terminal.pressure_cell(db, "ap-jp", H100)
    terminal.anomalies_view(db, None, None, None)
    terminal.procure_plan(db, domain.Workload(gpu_model_ids=[H100], duration_hours=10, interruption_tolerance="none", gpu_count=1))
    rows = db.query(Observation).all()
    assert rows, "the RunPod and Akash asks and the live indices were archived, so the reads did write"
    assert not [r.series_id for r in rows if r.series_id.startswith("offer:vast-")]
    assert not [r.series_id for r in rows if (r.provenance or {}).get("source") == "vast-ai"]
    assert not [r.series_id for r in rows if "vast-ai" in ((r.provenance or {}).get("input_sources") or [])]
    index_rows = [r for r in rows if r.series_id == f"index:fcpi:{H100}"]
    assert index_rows and all(r.provenance["input_sources"] == ["akash", "runpod"] for r in index_rows)


def test_vast_changes_are_not_reported_as_new_on_every_read(db, monkeypatch):
    _sources(monkeypatch)
    terminal.changes(db, None)
    again = terminal.changes(db, None)
    assert not [c for c in again["changes"] if c.offer_id.startswith("vast-")]


def test_the_archive_refuses_a_vast_ask_and_an_index_point_whose_basket_names_vast(db, monkeypatch):
    monkeypatch.setattr(archive, "utcnow", lambda: NOW)
    t = FETCHED
    ask = domain.PriceObservation("offer:vast-59433335:price", t, 2.3348, "USD/GPU-hour", VAST_ROWS[0].provenance)
    point = domain.PriceObservation(f"index:fcpi:{H100}", t, 2.1, "USD/GPU-hour", live("runpod", t, fetched_at=t))
    assert archive.record(db, [ask]) == 0
    assert archive.record(db, [point], baskets={point.series_id: ["runpod", "vast-ai"]}) == 0
    assert archive.record_results(db, [available("vast-ai", VAST_ROWS, VAST_ROWS[0].provenance)], archive.observations_from_offers, label="offers") == 0
    assert db.query(Observation).count() == 0
    envelope = {"kind": "derived", "source": "fcpi-v1:median-of-adjusted-asks", "observed_at": t.isoformat(), "input_sources": ["runpod", "vast-ai"]}
    assert archive.refusal(point.series_id, t, envelope, now=NOW) == "basket-not-indexable"
    assert archive.refusal("offer:vast-1:price", t, {"kind": "live", "source": "vast-ai", "observed_at": t.isoformat()}, now=NOW) == "source-off"


def test_the_sources_page_lists_vast_with_its_line():
    row = next(r for r in licences.rows() if r["id"] == "vast-ai")
    assert row["attribution"] == LINE and row["use"] == "display-not-index"
