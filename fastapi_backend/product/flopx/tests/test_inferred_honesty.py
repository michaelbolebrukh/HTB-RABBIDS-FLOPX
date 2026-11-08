"""Inferred-state honesty at the route composition (falsifier wave V1, findings F1, F4 and F6).

Adapters are replaced at their module seam (never the network): RunPod answers with
four live H100 asks in Texas spread wide enough to fire the price-dispersion rule,
Wikidata answers with live facility records for a region the sample set covers, and
every other source is unavailable. The clock is moved between reads so that a
timestamp taken from the read itself would show up as a moving `firstSeen`.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.exceptions import UnknownEntityError
from product.flopx import domain, terminal
from product.flopx.provenance import live
from product.flopx.sources import akash, available, energy, kalshi, news, polymarket, runpod, sec, unavailable, vast, wikidata

T_LIVE = datetime(2026, 11, 7, 15, 51, 8, tzinfo=timezone.utc)
H100 = "h100-sxm-80gb"


def _live_offer(i: int, price: float) -> domain.GpuOffer:
    return domain.GpuOffer(
        id=f"runpod-{i}", provider_id="runpod", region_id="us-tx", gpu_model_id=H100, gpu_count=8, price_per_gpu_hour_usd=price,
        availability="available", interruptible=False, contract="on-demand", provenance=live("runpod", T_LIVE, licence="test"),
    )


def _wikidata_facility(fid: str, region: str, status: str, mw: float) -> domain.Facility:
    return domain.Facility(id=fid, name=fid, lat=0, lon=0, region_id=region, status=status, capacity_mw=mw, provenance=live("wikidata", T_LIVE, licence="CC0"))


LIVE_OFFERS = [_live_offer(1, 1.0), _live_offer(2, 1.1), _live_offer(3, 3.0), _live_offer(4, 4.0)]
LIVE_FACILITIES = [_wikidata_facility("Q900001", "us-va", "operating", 9000.0), _wikidata_facility("Q900002", "us-va", "announced", 40000.0)]


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
    now = {"t": T_LIVE + timedelta(seconds=30)}
    monkeypatch.setattr(terminal, "utcnow", lambda: now["t"])
    return now


@pytest.fixture
def live_sources(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(2048))
    monkeypatch.setattr(vast, "fetch_gpu_offers", lambda: unavailable("vast-ai", "timeout"))   # these cases isolate RunPod; Vast has its own file
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", LIVE_OFFERS, live("runpod", T_LIVE, licence="test")))
    monkeypatch.setattr(akash, "fetch_gpu_offers", lambda: unavailable("akash", "timeout"))
    monkeypatch.setattr(wikidata, "fetch_facilities", lambda: available("wikidata", LIVE_FACILITIES, live("wikidata", T_LIVE, licence="CC0")))
    monkeypatch.setattr(polymarket, "fetch_prediction_markets", lambda: unavailable("polymarket-gamma", "timeout"))
    monkeypatch.setattr(kalshi, "fetch_series", lambda series: unavailable(f"kalshi:{series}", "timeout"))
    monkeypatch.setattr(news, "fetch_hn", lambda query=None, limit=20: unavailable("hacker-news", "timeout"))
    monkeypatch.setattr(news, "fetch_rss", lambda key, limit=10: unavailable(f"rss:{key}", "timeout"))
    monkeypatch.setattr(sec, "fetch_filings", lambda cik, ticker=None, limit=10: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(sec, "fetch_fundamentals", lambda cik, ticker=None: unavailable("sec-edgar", "timeout"))
    monkeypatch.setattr(energy, "fetch_energy_charts", lambda zone="DE-LU", days=1: unavailable("energy-charts", "timeout"))
    monkeypatch.setattr(energy, "fetch_elexon", lambda days=1: unavailable("elexon-bmrs", "timeout"))


def _next_window():
    """A fresh cache: the composed 60 s snapshot is gone, so the next read recomposes from the sources."""
    cache_module.cache._backend = MemoryBackend(2048)


def _dispersion(payload: dict) -> domain.Anomaly:
    rows = [a for a in payload["anomalies"] if a.id == f"anomaly:price-dispersion:us-tx:{H100}"]
    assert len(rows) == 1, [a.id for a in payload["anomalies"]]
    return rows[0]


def test_first_seen_is_stable_across_reads_and_is_never_the_later_read_time(db, clock, live_sources):
    first_read_at = clock["t"]
    first = _dispersion(terminal.anomalies_view(db, None, None, None))
    clock["t"] = first_read_at + timedelta(minutes=7)
    _next_window()
    second = _dispersion(terminal.anomalies_view(db, None, None, None))
    assert first.first_seen == first_read_at                       # nothing on record yet: the snapshot it was found in
    assert second.first_seen == first_read_at                      # the archived first detection, not the second read's time
    assert "first detected" in second.first_seen_basis
    clock["t"] = first_read_at + timedelta(minutes=14)
    _next_window()
    signal = terminal.signal_detail(db, None, f"signal:anomaly:{second.id}")
    assert signal["at"] == first_read_at


def test_supply_and_pipeline_never_sum_live_and_sample_megawatts(db, clock, live_sources):
    supply = terminal.effective_supply_view(db, "us-va")["regions"][0]
    assert supply.input_kinds == ["sample"]
    assert "Q900001" not in {f.facility_id for f in supply.facilities}
    assert "2 observed records" in supply.provenance.note
    cell = terminal.pressure_cell(db, "us-va", H100)
    pipeline = next(f for f in cell["factors"] if f.factor == "capacity-pipeline")
    assert {p.kind for p in pipeline.evidence} == {"sample"}


def test_brief_evidence_carries_provenance_with_input_kinds(db, clock, live_sources):
    brief = terminal.ask("what is happening in us-tx", db)["brief"]
    refs = {r.id: r for r in brief["evidence"]}
    derived_refs = [r for r in refs.values() if r.provenance is not None and r.provenance.kind == "derived"]
    assert derived_refs, "the brief cites at least one inference"
    for ref in derived_refs:
        # The ref states what the inference rests on, not just "derived".
        assert ref.input_kinds == sorted(ref.provenance.input_kinds), ref.id
    supply_ref = refs["supply:us-tx"]
    assert supply_ref.input_kinds == ["sample"] and supply_ref.provenance.kind == "derived"
    pressure_refs = [r for r in derived_refs if r.kind == "pressure-cell"]
    assert pressure_refs and all("sample" in r.input_kinds for r in pressure_refs)   # sample series and feed sit under every cell here


def test_a_factor_is_a_driver_or_a_contradiction_never_both():
    # V4 F19: "driven mainly by price-premium (+1.00)" and "arguing against: price-premium (+1.00)" in one brief.
    from dataclasses import replace as dc_replace
    from product.flopx import assistant, pressure
    from product.flopx.provenance import sample
    model = domain.GpuModel(id=H100, vendor="NVIDIA", family="Hopper", memory_gb=80, interconnect="NVLink", fp16_tflops=989.0, release_year=2022)
    region = domain.Region(id="us-tx", name="Texas", country_code="US", lat=0, lon=0, grid_zone="ERCOT")
    offers = [domain.GpuOffer(id=f"o{i}", provider_id="p", region_id="us-tx", gpu_model_id=H100, gpu_count=8, price_per_gpu_hour_usd=3.0,
                              availability="available", interruptible=False, contract="on-demand", provenance=sample(T_LIVE)) for i in range(4)]
    base = pressure.compute_cell("us-tx", H100, offers=offers, gpu_models={H100: model}, regions={"us-tx": region}, series={}, facilities=[],
                                 markets=[], feed=[], as_of=T_LIVE)
    premium = next(f for f in base.factors if f.factor == "price-premium")
    avail = next(f for f in base.factors if f.factor == "availability")
    up = dc_replace(premium, raw=0.3, normalised=1.0, contribution=0.3, direction="up")
    down = dc_replace(avail, raw=0.0, normalised=-1.0, contribution=-0.2, direction="down")
    cell = dc_replace(base, score=48.0, band="balanced", factors=[up, down, *base.factors[2:]], contradictions=[up])
    brief = assistant.compose(target_kind="region", target_id="us-tx", target_name="Texas", cells=[cell], anomalies=[], supply=None, feed=[],
                              markets=[], latest=[], evaluation=[], exposures=[])
    pressure_sentence, against = brief.sentences[0].text, brief.sentences[1].text
    assert "driven mainly by availability (-1.00)" in pressure_sentence and "price-premium" not in pressure_sentence
    assert against.startswith("Arguing against that reading: price-premium (+1.00)")


def test_an_anomaly_that_stops_firing_stays_addressable_as_resolved(db, clock, live_sources, monkeypatch):
    # V4 F26: a link copied minutes earlier landed on Not found once the rule stopped holding.
    first_read_at = clock["t"]
    found = _dispersion(terminal.anomalies_view(db, None, None, None))
    tight = [_live_offer(i, 2.0 + i / 100) for i in range(1, 5)]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", tight, live("runpod", T_LIVE, licence="test")))
    clock["t"] = first_read_at + timedelta(minutes=3)
    _next_window_keeping_last_seen()
    assert all(a.id != found.id for a in terminal.anomalies_view(db, None, None, None)["anomalies"])
    detail = terminal.anomaly_detail(db, found.id)
    assert detail["id"] == found.id and detail["status"] == "resolved"
    assert detail["last_seen"] == first_read_at and detail["first_seen"] == found.first_seen
    assert detail["cell"] is not None                     # the current cell: why it no longer holds
    signal = terminal.signal_detail(db, None, f"signal:anomaly:{found.id}")
    assert signal["anomaly"].status == "resolved"
    with pytest.raises(UnknownEntityError):
        terminal.anomaly_detail(db, "anomaly:bottleneck:us-tx:never-seen")


def _next_window_keeping_last_seen():
    """Expire the 60 s composed snapshot only; the day-long last-seen entries survive, as they would in the cache."""
    cache_module.cache.delete(terminal.INFERRED_SNAPSHOT_KEY)


def test_live_asks_with_no_region_never_empty_the_regional_grid(db, clock, live_sources, monkeypatch):
    """RunPod and Akash publish no region. With only such asks live, a model's regional cells keep its
    sample asks and say so: the grid is never emptied by asks that cannot sit in any cell (re-walk
    after b5cb576, where every cell on both tiers read coverage 0)."""
    global_asks = [replace_region(o, "global") for o in LIVE_OFFERS]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", global_asks, live("runpod", T_LIVE, licence="test")))
    _next_window()
    cells = terminal.pressure_grid(db, H100, None)["cells"]
    covered = [c for c in cells if c.uncertainty.coverage > 0]
    assert covered, "every H100 cell lost its asks"
    for cell in covered:
        premium = next(f for f in cell.factors if f.factor == "price-premium")
        assert {p.kind for p in premium.evidence} == {"sample"}, cell.region_id


def test_live_asks_placed_in_a_region_replace_the_sample_asks_for_that_model(db, clock, live_sources):
    _next_window()
    cells = {c.region_id: c for c in terminal.pressure_grid(db, H100, None)["cells"]}
    premium = next(f for f in cells["us-tx"].factors if f.factor == "price-premium")
    assert {p.kind for p in premium.evidence} == {"live"}
    assert cells["us-va"].uncertainty.coverage == 0                 # no sample asks mixed in beside the live ones


def replace_region(offer: domain.GpuOffer, region_id: str) -> domain.GpuOffer:
    from dataclasses import replace
    return replace(offer, region_id=region_id)
