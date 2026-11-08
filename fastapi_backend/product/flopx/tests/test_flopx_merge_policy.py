"""The merge policy: live where an adapter answered, sample otherwise, aggregates over one class.

Adapters are replaced at their module seam (never the network): RunPod answers
with live H100 offers, Akash is unavailable, Vast.ai (licence row
`display-not-index`, founder decision G9-b) answers with the recorded upstream
body parsed by the real adapter (H100 asks in Japan and India, RTX 4090 asks in
Texas), so every invariant below also holds with a market reading present that
may never enter an index, a heatmap median or the archive. Polymarket
answers with one market whose id collides with a sample market, the news feeds
answer with one primary-source release and one Hacker News story. The
assertions are the invariants from brief B1 deliverable D.
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
from product.flopx import domain, terminal
from product.flopx.data.loader import load_sample_uncached
from product.flopx.provenance import effective_kind, live
from product.flopx.sources import SourceResult, akash, available, energy, kalshi, news, polymarket, runpod, sec, unavailable, vast, wikidata, yahoo
from product.models import Observation

T_LIVE = datetime(2026, 11, 7, 15, 51, 8, tzinfo=timezone.utc)
T_MARKET = datetime(2026, 11, 7, 15, 50, 36, tzinfo=timezone.utc)


def _live_offer(i: int, price: float, *, region: str = "us-tx", model: str = "h100-sxm-80gb") -> domain.GpuOffer:
    return domain.GpuOffer(
        id=f"runpod-{i}", provider_id="runpod", region_id=region, gpu_model_id=model, gpu_count=8, price_per_gpu_hour_usd=price,
        availability="available", interruptible=False, contract="on-demand",
        provenance=live("runpod", T_LIVE, licence="test", note="advertised ask"),
    )


LIVE_OFFERS = [_live_offer(1, 2.4), _live_offer(2, 2.6, region="gb"), _live_offer(3, 2.5, region="us-va")]
RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
VAST_ROWS = vast.parse(json.loads((RECORDED / "vast" / "bundles.json").read_text(encoding="utf-8"))["body"], T_LIVE, T_LIVE)
VAST_LINE = "Vast.ai asks: a current market reading, not an index or benchmark"


def _market(market_id: str, question: str) -> domain.PredictionMarket:
    prov = live("polymarket-gamma", T_MARKET, licence="test")
    return domain.PredictionMarket(id=market_id, venue="polymarket", question=question, last_updated=T_MARKET, provenance=prov,
                                   outcomes=[domain.OutcomeQuote("Yes", 0.4, 0.4), domain.OutcomeQuote("No", 0.6, 0.6)], volume_24h=9999.0)


def _intel(item_id: str, source: str, classification: str) -> domain.IntelItem:
    t = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)
    return domain.IntelItem(id=item_id, kind="announcement", headline=f"{item_id} headline", why_it_matters="test", published_at=t, observed_at=t,
                            severity=2, classification=classification, provenance=live(source, t, licence="test"),
                            entities=domain.IntelEntities(tickers=["CRWV"]))


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
def mixed(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    monkeypatch.setattr(vast, "fetch_gpu_offers", lambda: available("vast-ai", VAST_ROWS, live("vast-ai", T_LIVE, licence="test")))
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", LIVE_OFFERS, live("runpod", T_LIVE, licence="test")))
    monkeypatch.setattr(akash, "fetch_gpu_offers", lambda: unavailable("akash", "timeout"))
    monkeypatch.setattr(polymarket, "fetch_prediction_markets", lambda: available("polymarket-gamma", [_market("mkt-001", "Live twin of the sample market"), _market("polymarket:1", "A new question")], live("polymarket-gamma", T_MARKET)))
    monkeypatch.setattr(kalshi, "fetch_series", lambda series: unavailable(f"kalshi:{series}", "no-open-markets"))
    monkeypatch.setattr(news, "fetch_hn", lambda query=None, limit=20: available("hacker-news", [_intel("hn:1", "hacker-news", "interpretation")], live("hacker-news", T_LIVE)))
    monkeypatch.setattr(news, "fetch_rss", lambda key, limit=10: available(f"rss:{key}", [_intel(f"rss:{key}:1", f"rss:{key}", "verified")], live(f"rss:{key}", T_LIVE)) if key == "coreweave" else unavailable(f"rss:{key}", "http-403"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "http-403"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "http-403"))
    monkeypatch.setattr(sec, "fetch_frame", lambda concept="capex", period=None: unavailable("sec-edgar", "http-403"))
    monkeypatch.setattr(energy, "fetch_energy_charts", lambda zone="DE-LU", days=1: unavailable("energy-charts", "timeout"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: unavailable("elexon-bmrs", "timeout"))
    monkeypatch.setattr(wikidata, "fetch_facilities", lambda: unavailable("wikidata", "http-429"))
    monkeypatch.setattr(yahoo, "fetch_bars", lambda t, r="6mo", i="1d": unavailable("yahoo-finance", "http-429"))
    return LIVE_OFFERS


def test_offers_mix_rows_but_each_row_carries_its_own_provenance(db, mixed):
    body = terminal.offers(db, "h100-sxm-80gb", None, None, None, "adjusted")
    kinds = {o["provenance"].kind for o in body["offers"]}
    assert kinds == {"live", "sample"}
    assert {o["provider_id"] for o in body["offers"]} >= {"runpod", "vast-ai"}
    vast_rows = [o for o in body["offers"] if o["provider_id"] == "vast-ai"]
    assert {o["id"] for o in vast_rows} == {"vast-54322834", "vast-59433335"}               # the two recorded H100 SXM asks
    assert all(o["provenance"].note.startswith(VAST_LINE) for o in vast_rows)               # each row says it is a market reading
    fresh = body["freshness"]
    assert "runpod" in fresh.sources and "vast-ai" in fresh.sources and "flopx-sample-v1" in fresh.sources
    assert {u["source"]: u["reason"] for u in fresh.unavailable} == {"akash": "timeout"}
    assert [d["source"] for d in fresh.source_details] == ["runpod", "vast-ai"]
    assert fresh.source_details[0]["observed_at"] == T_LIVE.isoformat()
    assert not db.query(Observation).filter(Observation.series_id.like("offer:vast-%")).count()   # shown, never archived


def test_indices_are_live_only_where_live_offers_exist_and_sample_elsewhere(db, mixed):
    body = terminal.indices(db)
    by_model = {ix.gpu_model_id: ix for ix in body["indices"]}
    h100, b200 = by_model["h100-sxm-80gb"], by_model["b200-sxm-192gb"]
    assert h100.provenance.input_kinds == ("live",) and effective_kind(h100.provenance) == "derived"
    assert h100.current == 2.5 and h100.coverage.offer_count == 3 and h100.coverage.regions == 3    # median of the three RunPod asks only
    assert h100.basket == ["runpod"]                                                                  # the Vast reading never enters a basket
    assert b200.provenance.input_kinds == ("sample",) and effective_kind(b200.provenance) == "sample"
    assert any("FLOPX-collected" in limitation for limitation in h100.limitations)
    assert h100.values[-1].value == 2.5 and h100.values[-1].provenance.kind == "derived"
    fresh = body["freshness"]
    assert "fcpi-v1:median-of-adjusted-asks" in fresh.sources and {u["source"] for u in fresh.unavailable} == {"akash"}
    assert "vast-ai" not in fresh.sources                                                             # the index read does not draw on it


def test_a_live_index_is_archived_once_and_its_history_grows_from_the_archive(db, mixed, monkeypatch):
    terminal.index(db, "h100-sxm-80gb")
    rows = db.query(Observation).filter(Observation.series_id == "index:fcpi:h100-sxm-80gb").all()
    assert len(rows) == 1 and rows[0].value == 2.5 and rows[0].provenance["input_sources"] == ["runpod"]
    assert db.query(Observation).filter(Observation.series_id == "offer:runpod-1:price").count() == 1
    terminal.index(db, "h100-sxm-80gb")  # the same observation again: nothing new
    assert db.query(Observation).filter(Observation.series_id == "index:fcpi:h100-sxm-80gb").count() == 1
    later = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)
    newer = [domain.GpuOffer(**{**o.__dict__, "price_per_gpu_hour_usd": o.price_per_gpu_hour_usd + 1.0, "provenance": live("runpod", later, licence="test")}) for o in LIVE_OFFERS]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", newer, live("runpod", later, licence="test")))
    ix = terminal.index(db, "h100-sxm-80gb")
    assert [p.value for p in ix["values"]] == [2.5, 3.5]
    assert ix["values"][0].provenance.kind == "archived" and ix["values"][1].provenance.kind == "derived"
    # The single-index read carries its own freshness block (V6 #8): the live asks behind it and the failed marketplace.
    assert "runpod" in ix["freshness"].sources and {u["source"] for u in ix["freshness"].unavailable} == {"akash"}


def test_heatmap_cells_are_computed_over_one_class_and_say_which(db, mixed):
    body = terminal.heatmap(db, "median-raw-price", "region")
    cells = {(c["gpu_model_id"], c["region_id"]): c for c in body["cells"]}
    assert cells[("h100-sxm-80gb", "us-tx")]["kind"] == "live" and cells[("h100-sxm-80gb", "us-tx")]["value"] == 2.4 and cells[("h100-sxm-80gb", "us-tx")]["n"] == 1
    assert cells[("h100-sxm-80gb", "gb")]["kind"] == "live"                        # a live-only region joins the axis
    assert cells[("b200-sxm-192gb", "us-tx")]["kind"] in ("sample", None)
    sample_h100_tx = [o for o in load_sample_uncached().offers if o.gpu_model_id == "h100-sxm-80gb" and o.region_id == "us-tx"]
    assert sample_h100_tx, "the sample set has H100 asks in Texas, which the live cell must not absorb"
    assert body["legend"]["kind"] == "mixed-per-cell" and body["legend"]["liveCells"] >= 3
    assert "live_cells" not in body["legend"] and "sampleCells" in body["legend"]       # dict keys are camelCase on the wire too (V6 #14)
    # A heatmap cell is a cross-provider median: the Vast reading (H100 in Japan, RTX 4090 in Texas) stays out of it.
    assert cells[("h100-sxm-80gb", "ap-jp")]["kind"] != "live" and ("h100-sxm-80gb", "in") not in cells
    assert cells[("rtx-4090-24gb", "us-tx")]["kind"] != "live"
    assert "vast-ai" not in body["freshness"].sources


def test_overview_snapshot_and_indicators_follow_the_same_rule(db, mixed):
    body = terminal.overview(db, None)
    snapshot = {row["gpu_model_id"]: row for row in body["gpu_snapshot"]}
    assert snapshot["h100-sxm-80gb"]["offers"] == 3 and snapshot["h100-sxm-80gb"]["provenance"].input_kinds == ("live",)   # RunPod only, no Vast
    assert snapshot["rtx-4090-24gb"]["provenance"].input_kinds == ("sample",)                         # only Vast answered for it
    assert snapshot["b200-sxm-192gb"]["provenance"].input_kinds == ("sample",)
    fcpi = {i["id"]: i for i in body["indicators"] if i["id"].startswith("index:fcpi:")}
    assert fcpi["index:fcpi:h100-sxm-80gb"]["provenance"].input_kinds == ("live",)
    assert fcpi["index:fcpi:a100-sxm-80gb"]["provenance"].input_kinds == ("sample",)
    # The watchlist keeps the sample close when Yahoo did not answer, and the freshness says why:
    assert all(row["last_close"].provenance.kind == "sample" for row in body["watchlist"])
    assert any(u["source"] == "yahoo-finance" and "http-429" in u["reason"] for u in body["freshness"].unavailable)
    assert {"akash", "energy-charts", "elexon-bmrs"} <= {u["source"] for u in body["freshness"].unavailable}


def test_markets_replace_on_id_match_and_append_otherwise(db, mixed):
    rows = terminal.markets(db)["markets"]
    ids = [m.id for m in rows]
    assert ids.count("mkt-001") == 1 and next(m for m in rows if m.id == "mkt-001").provenance.kind == "live"
    assert "polymarket:1" in ids and len(rows) == 12 + 1
    assert db.query(Observation).filter(Observation.series_id == "market:polymarket:1:yes").count() == 1
    detail = terminal.market(db, "mkt-002")
    assert detail.provenance.kind == "sample"


def test_feed_classification_follows_the_primary_source_rule(db, mixed):
    rows = terminal.feed(db, None, "CRWV", None, None, None)["items"]
    by_id = {i.id: i for i in rows}
    assert by_id["rss:coreweave:1"].classification == "verified"
    assert by_id["hn:1"].classification == "interpretation"
    assert rows == sorted(rows, key=lambda i: i.published_at, reverse=True)
    fresh = terminal.feed(db, None, None, None, None, None)["freshness"]
    assert {u["source"] for u in fresh.unavailable} >= {"rss:nvidia", "rss:corescientific", "rss:applieddigital", "sec-edgar"}


def test_procurement_ranks_live_and_sample_asks_together_and_labels_the_mix(db, mixed):
    workload = domain.Workload(gpu_model_ids=["h100-sxm-80gb"], duration_hours=10, interruption_tolerance="none", gpu_count=8)
    body = terminal.procure_plan(db, workload)
    plan_ids = [p.offer_id for p in body["plans"]]
    assert "runpod-1" in plan_ids
    assert set(body["provenance"].input_kinds) <= {"live", "sample"} and "live" in body["provenance"].input_kinds


def test_everything_falls_back_to_sample_when_nothing_answers(db, monkeypatch, mixed):
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: unavailable("runpod", "network-error"))
    body = terminal.indices(db)
    assert all(ix.provenance.input_kinds == ("sample",) for ix in body["indices"])
    assert {u["source"] for u in body["freshness"].unavailable} == {"runpod", "akash"}
    # Vast answered, yet no index is live and nothing was archived: a market reading alone builds no index.
    assert db.query(Observation).count() == 0


def test_changes_treat_the_same_observation_as_unchanged_and_a_later_move_as_a_change(db, mixed, monkeypatch):
    first = terminal.changes(db, None)
    live_changes = [c for c in first["changes"] if c.offer_id.startswith("runpod-")]
    assert {c.kind for c in live_changes} == {"new"} and len(live_changes) == 3        # nothing archived yet: all new
    assert not [c for c in first["changes"] if c.offer_id.startswith("vast-")]         # never archived, so no move can be read
    again = terminal.changes(db, None)
    assert not [c for c in again["changes"] if c.offer_id.startswith("runpod-")]         # same fetch, same observation: no news
    later = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)
    moved = [LIVE_OFFERS[0].__class__(**{**LIVE_OFFERS[0].__dict__, "price_per_gpu_hour_usd": 3.0, "provenance": live("runpod", later, licence="test")}),
             LIVE_OFFERS[1].__class__(**{**LIVE_OFFERS[1].__dict__, "availability": "limited", "provenance": live("runpod", later, licence="test")})]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", moved, live("runpod", later, licence="test")))
    third = terminal.changes(db, None)
    kinds = {(c.kind, c.offer_id) for c in third["changes"] if c.offer_id.startswith("runpod-")}
    # A live offer that disappeared (runpod-3) is NOT reported: the archive holds numeric observations
    # only, not the offer's provider/model/region, so a removal cannot be reconstructed honestly.
    assert kinds == {("price", "runpod-1"), ("availability", "runpod-2")}
    price = next(c for c in third["changes"] if c.offer_id == "runpod-1")
    assert price.before["pricePerGpuHourUsd"] == 2.4 and price.after["pricePerGpuHourUsd"] == 3.0 and price.pct_change == 25.0
    assert "runpod" in third["freshness"].sources and third["until"] == later
