"""cpx-v1 against hand-computed cells (architecture §10.2).

Two regions, one model, four on-demand asks in the US (region factor 1.0, no
memory adjustment, so comparable = quoted):
  us-tx: 3.0 (available), 3.6 (limited)      us-va: 2.0, 2.4 (both available)
  global median = median(3.0, 3.6, 2.0, 2.4) = 2.7; us-tx median = 3.3
  price-premium raw = 3.3 / 2.7 - 1 = 0.2222 -> normalised 0.2222 / 0.30 = 0.7407
  availability raw = 0 + 0.5 x 0.5 = 0.25 -> normalised 2 x 0.25 - 1 = -0.5
  no series, facilities, markets or feed -> the other four factors have no data
  score = 50 + 50 x (0.30 x 0.7407 + 0.20 x -0.5) / 0.50 = 50 + 50 x 0.2444 = 62.22 (tight)
"""

from datetime import datetime, timedelta, timezone

import pytest

from product.flopx import domain, pressure
from product.flopx.provenance import Provenance, live, sample

T0 = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
H100 = domain.GpuModel(id="h100-sxm-80gb", vendor="NVIDIA", family="Hopper", memory_gb=80, interconnect="NVLink", fp16_tflops=989.0, release_year=2022)
REGIONS = {
    "us-tx": domain.Region(id="us-tx", name="Texas", country_code="US", lat=32.0, lon=-96.0, grid_zone="ERCOT"),
    "us-va": domain.Region(id="us-va", name="Virginia", country_code="US", lat=39.0, lon=-77.0, grid_zone="PJM"),
}


def offer(i, price, region, availability="available", prov=None):
    return domain.GpuOffer(id=f"o{i}", provider_id="p", region_id=region, gpu_model_id=H100.id, gpu_count=8, price_per_gpu_hour_usd=price,
                           availability=availability, interruptible=False, contract="on-demand", provenance=prov or sample(T0))


OFFERS = [offer(1, 3.0, "us-tx"), offer(2, 3.6, "us-tx", "limited"), offer(3, 2.0, "us-va"), offer(4, 2.4, "us-va")]


def series(sid, values, unit="USD/GPU-hour", kind="sample", start=T0 - timedelta(days=40)):
    points = [domain.PriceObservation(series_id=sid, t=start + timedelta(days=i), value=v, unit=unit,
                                      provenance=Provenance(kind=kind, source="test", observed_at=start + timedelta(days=i))) for i, v in enumerate(values)]
    return domain.Series(id=sid, unit=unit, points=points)


def cell(**overrides):
    kwargs = dict(offers=OFFERS, gpu_models={H100.id: H100}, regions=REGIONS, series={}, facilities=[], markets=[], feed=[], as_of=T0)
    kwargs.update(overrides)
    return pressure.compute_cell("us-tx", H100.id, **kwargs)


def test_two_factor_cell_matches_the_hand_computation():
    c = cell()
    by = {f.factor: f for f in c.factors}
    assert by["price-premium"].raw == pytest.approx(0.2222, abs=1e-4) and by["price-premium"].normalised == pytest.approx(0.7407, abs=1e-4)
    assert by["availability"].raw == 0.25 and by["availability"].normalised == -0.5
    assert all(by[f].normalised is None and by[f].direction == "none" for f in ("price-momentum", "power-stress", "capacity-pipeline", "demand-signals"))
    assert c.score == pytest.approx(62.22, abs=0.01) and c.band == "tight"
    assert [f.factor for f in c.contradictions] == ["availability"]
    assert c.uncertainty.coverage == 2 and c.uncertainty.factors_with_data == 2 and c.uncertainty.label == "exploratory" and c.uncertainty.sample_share == 1.0
    assert c.statement_class == "inference" and c.method == "cpx-v1" and "not" in c.method_text and "measure" in c.method_text
    assert c.provenance.kind == "derived" and c.input_kinds == ["sample"]


def test_weights_renormalise_over_factors_with_data():
    c = cell()
    with_data = [f for f in c.factors if f.normalised is not None]
    assert sum(f.weight for f in with_data) == pytest.approx(0.5)
    assert c.score == pytest.approx(50 + 50 * sum(f.contribution for f in with_data) / 0.5, abs=0.01)


def test_momentum_power_pipeline_and_demand_each_match_their_formula():
    momentum = series("gpu:h100-sxm-80gb:on-demand:global", [2.0] * 40 + [2.3])          # +15% over 30 days -> normalised exactly 1.0
    power = series("energy:ERCOT:day-ahead", [40.0] * 40 + [50.0], unit="USD/MWh")       # latest 50 vs trailing median 40 -> raw 0.25 -> 0.5
    facilities = [
        domain.Facility(id="f1", name="op", lat=0, lon=0, region_id="us-tx", status="operating", capacity_mw=100, provenance=sample(T0)),
        domain.Facility(id="f2", name="plan", lat=0, lon=0, region_id="us-tx", status="planned", capacity_mw=300, provenance=sample(T0)),
        domain.Facility(id="f3", name="rep", lat=0, lon=0, region_id="us-tx", status="reported", capacity_mw=900, provenance=sample(T0)),
    ]
    market = domain.PredictionMarket(id="m1", venue="v", question="H100 on-demand median price above $3.00/GPU-hour at end of December?",
                                     outcomes=[domain.OutcomeQuote("Yes", 0.75, 0.75)], last_updated=T0 - timedelta(days=1), provenance=sample(T0))
    item = domain.IntelItem(id="i1", kind="power-event", headline="h", why_it_matters="", published_at=T0, observed_at=T0 - timedelta(days=2), severity=5,
                            classification="verified", provenance=sample(T0), geography=domain.Geography(region_ids=["us-tx"]))
    c = cell(series={momentum.id: momentum, power.id: power}, facilities=facilities, markets=[market], feed=[item])
    by = {f.factor: f for f in c.factors}
    assert by["price-momentum"].raw == pytest.approx(0.15) and by["price-momentum"].normalised == 1.0 and by["price-momentum"].scope == "global"
    assert by["power-stress"].raw == pytest.approx(0.25) and by["power-stress"].normalised == 0.5
    assert by["capacity-pipeline"].raw == 3.0 and by["capacity-pipeline"].normalised == -1.0 and by["capacity-pipeline"].direction == "down"
    # demand = mean((0.75 - 0.5) x 2, clamp(5/5 / 5, 0, 1)) = mean(0.5, 0.2) = 0.35
    assert by["demand-signals"].raw == pytest.approx(0.35) and by["demand-signals"].normalised == pytest.approx(0.35)
    assert c.uncertainty.factors_with_data == 6
    expected = 50 + 50 * (0.3 * 0.7407 + 0.2 * -0.5 + 0.15 * 1.0 + 0.15 * 0.5 + 0.1 * -1.0 + 0.1 * 0.35) / 1.0
    assert c.score == pytest.approx(expected, abs=0.02)


def test_price_down_market_is_inverted_and_unrelated_markets_are_ignored():
    down = domain.PredictionMarket(id="m2", venue="v", question="H100 on-demand median price below $3.00/GPU-hour at end of December?",
                                   outcomes=[domain.OutcomeQuote("Yes", 0.8, 0.8)], last_updated=T0, provenance=sample(T0))
    other = domain.PredictionMarket(id="m3", venue="v", question="Texas announces a moratorium before July 2027?",
                                    outcomes=[domain.OutcomeQuote("Yes", 0.3, 0.3)], last_updated=T0, provenance=sample(T0))
    assert pressure.price_up_probability(down, H100.id, T0)[0] == pytest.approx(0.2)
    assert pressure.price_up_probability(other, H100.id, T0)[0] is None
    # A market only known later than `at` falls back to its history, or to nothing:
    future = domain.PredictionMarket(id="m4", venue="v", question="H100 price above $3?", outcomes=[domain.OutcomeQuote("Yes", 0.9, 0.9)],
                                     last_updated=T0 + timedelta(days=1), provenance=sample(T0))
    assert pressure.price_up_probability(future, H100.id, T0)[0] is None


def test_band_edges():
    assert [pressure.band_for(x) for x in (0, 24.99, 25, 50, 50.01, 74.99, 75, 100)] == ["slack", "slack", "balanced", "balanced", "tight", "tight", "acute", "acute"]


def test_empty_cell_has_coverage_zero_and_sits_at_the_centre():
    c = pressure.compute_cell("us-va", H100.id, offers=[o for o in OFFERS if o.region_id == "us-tx"], gpu_models={H100.id: H100}, regions=REGIONS,
                              series={}, facilities=[], markets=[], feed=[], as_of=T0)
    assert c.uncertainty.coverage == 0 and c.uncertainty.factors_with_data == 0 and c.score == 50.0 and c.band == "balanced" and c.contradictions == []


def test_live_inputs_make_a_derived_cell_and_enough_of_them_make_it_indicative():
    prov = live("vast-ai", T0, licence="test")
    offers = [offer(i, 3.0 + i / 10, "us-tx", prov=prov) for i in range(1, 4)] + [offer(9, 2.5, "us-va", prov=prov)]
    momentum = series("index:fcpi:h100-sxm-80gb", [2.0] * 40 + [2.1], kind="archived")
    power = series("energy:ERCOT:day-ahead", [40.0] * 40 + [41.0], unit="USD/MWh", kind="live")
    facilities = [domain.Facility(id="f1", name="op", lat=0, lon=0, region_id="us-tx", status="operating", capacity_mw=100, provenance=live("wikidata", T0))]
    c = cell(offers=offers, series={momentum.id: momentum, power.id: power}, facilities=facilities)
    assert c.input_kinds == ["archived", "live"] and c.provenance.kind == "derived"
    assert c.uncertainty.coverage == 3 and c.uncertainty.factors_with_data == 5 and c.uncertainty.sample_share == 0.0
    assert c.uncertainty.label == "indicative"
    assert c.uncertainty.label != "measured"


# -- N-GDH-BE: momentum may read GetDeploying's weekly median while FLOPX's own index is too short -----------------
#
# Weeks are stamped at their end (Monday 00:00 UTC, the loader's rule); the last week known at T0 (Friday 2026-11-06
# 21:00) ends 2026-11-02, the window reaches back to 2026-10-03, and the latest week end at or before that is
# 2026-09-28: 35 days apart, five weeks. Values are 3.0 every week except 3.3 for the last, so raw = 3.3 / 3.0 - 1 = 0.10
# and normalised = 0.10 / 0.15 = 0.6667.

WEEKLY_ID = "gpu:h100-sxm-80gb:getdeploying:weekly-median"
FCPI_ID = "index:fcpi:h100-sxm-80gb"
GD_NOTE = ("30-day change of GetDeploying's weekly median (CC BY 4.0), FLOPX index history too short (5 days); "
           "gpu:h100-sxm-80gb:getdeploying:weekly-median: 3.3000 (week ending 2026-11-02) vs 3.0000 (week ending 2026-09-28), 35 days apart")


def weekly(values, *, last_week_end=datetime(2026, 11, 2, tzinfo=timezone.utc), sid=WEEKLY_ID):
    """GetDeploying weekly medians as the archive serves them: archived, source getdeploying, CC BY 4.0, one point per week end."""
    starts = [last_week_end - timedelta(days=7 * (len(values) - 1 - i)) for i in range(len(values))]
    points = [domain.PriceObservation(series_id=sid, t=t, value=v, unit="USD/GPU-hour",
                                      provenance=Provenance(kind="archived", source="getdeploying", observed_at=t, licence="CC BY 4.0",
                                                            note="GetDeploying (2026). GPU rental price history [dataset], CC BY 4.0"))
              for t, v in zip(starts, values)]
    return domain.Series(id=sid, unit="USD/GPU-hour", points=points)


def fcpi(days, *, end=T0):
    """A FLOPX index with one archived point a day, the last at `end`: `days` distinct days of history."""
    return series(FCPI_ID, [2.4 + i / 100 for i in range(days)], kind="archived", start=end - timedelta(days=days - 1))


def test_momentum_falls_back_to_getdeploying_when_the_index_is_too_short_and_labels_it():
    young = fcpi(5)                                                   # 2026-11-02 .. 2026-11-06: five days, no 30-day change possible
    weeks = weekly([3.0] * 51 + [3.3])
    assert pressure.momentum_series_for({young.id: young, weeks.id: weeks}, H100.id, "us-tx", T0) == (weeks, "getdeploying-weekly")
    c = cell(series={young.id: young, weeks.id: weeks})
    f = {f.factor: f for f in c.factors}["price-momentum"]
    assert f.raw == pytest.approx(0.10) and f.normalised == pytest.approx(0.6667, abs=1e-4) and f.direction == "up"
    assert f.scope == "getdeploying-weekly" and f.note == GD_NOTE
    assert f.observed_at == datetime(2026, 11, 2, tzinfo=timezone.utc)
    # The evidence is the two GetDeploying points' own provenance, so the cell's kinds and sample share stay honest:
    assert [p.observed_at for p in f.evidence] == [datetime(2026, 9, 28, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc)]
    assert {p.kind for p in f.evidence} == {"archived"} and {p.source for p in f.evidence} == {"getdeploying"} and {p.licence for p in f.evidence} == {"CC BY 4.0"}
    assert c.input_kinds == ["archived", "sample"]
    # Four sample asks and two archived points: sample share 4 / 6.
    assert c.uncertainty.sample_share == pytest.approx(4 / 6, abs=1e-4)
    # No index at all: the note counts zero days of FLOPX history and the weekly median is still read.
    f0 = {f.factor: f for f in cell(series={weeks.id: weeks}).factors}["price-momentum"]
    assert f0.scope == "getdeploying-weekly" and f0.raw == pytest.approx(0.10) and "too short (0 days)" in f0.note


def test_an_index_spanning_the_window_is_preferred_and_the_note_is_unchanged():
    old = fcpi(40)                                                    # 2.40 .. 2.79; 30 days before the last is 2.49
    weeks = weekly([3.0] * 51 + [3.3])
    assert pressure.momentum_series_for({old.id: old, weeks.id: weeks}, H100.id, "us-tx", T0) == (old, "global")
    f = {f.factor: f for f in cell(series={old.id: old, weeks.id: weeks}).factors}["price-momentum"]
    assert f.scope == "global" and f.raw == pytest.approx(2.79 / 2.49 - 1, abs=1e-4)
    assert f.note == "index:fcpi:h100-sxm-80gb: 2.7900 vs 2.4900 30 days earlier (global series, no regional one exists)"
    assert {p.source for p in f.evidence} == {"test"} and "getdeploying" not in f.note


def test_the_fallback_fires_only_when_the_index_is_short_and_the_weekly_series_spans_the_window():
    young = fcpi(5)
    weeks = weekly([3.0] * 51 + [3.3])
    # A regional series still wins outright, however long or short (GetDeploying is global):
    regional = series("gpu:h100-sxm-80gb:on-demand:us-tx", [2.0, 2.1, 2.2], start=T0 - timedelta(days=2))
    assert pressure.momentum_series_for({regional.id: regional, young.id: young, weeks.id: weeks}, H100.id, "us-tx", T0) == (regional, None)
    # Two weeks of GetDeploying do not span 30 days: the short index is returned as before and momentum stays empty.
    two = weekly([3.0, 3.3])
    assert pressure.momentum_series_for({young.id: young, two.id: two}, H100.id, "us-tx", T0) == (young, "global")
    f = {f.factor: f for f in cell(series={young.id: young, two.id: two}).factors}["price-momentum"]
    assert f.normalised is None and f.scope == "global" and f.note == "index:fcpi:h100-sxm-80gb has fewer than 30 days of history before 2026-11-06"
    # No weekly series at all: exactly the pre-existing behaviour.
    assert pressure.momentum_series_for({young.id: young}, H100.id, "us-tx", T0) == (young, "global")
    assert pressure.momentum_series_for({}, H100.id, "us-tx", T0) == (None, None)
    # The whole-series judgement without `as_of`, and the daily-only chain a projection asks for:
    assert pressure.momentum_series_for({young.id: young, weeks.id: weeks}, H100.id, "us-tx") == (weeks, "getdeploying-weekly")
    assert pressure.momentum_series_for({young.id: young, weeks.id: weeks}, H100.id, "us-tx", T0, weekly_fallback=False) == (young, "global")
    # The sample path is untouched: the global sample series is read when nothing else exists, as every fixture expects.
    sample_global = series("gpu:h100-sxm-80gb:on-demand:global", [2.0] * 40 + [2.3])
    assert pressure.momentum_series_for({sample_global.id: sample_global}, H100.id, "us-tx", T0) == (sample_global, "global")
    # Real archived weeks outrank the authored sample series, which is not FLOPX history:
    assert pressure.momentum_series_for({sample_global.id: sample_global, weeks.id: weeks}, H100.id, "us-tx", T0) == (weeks, "getdeploying-weekly")


def test_history_judges_the_fallback_from_the_points_known_at_each_date():
    # An index of 40 days ending at T0 spans the window today, but 35 days ago only five of its days were known:
    old = fcpi(40)
    weeks = weekly([3.0] * 51 + [3.3])
    then = T0 - timedelta(days=35)
    assert pressure.momentum_series_for({old.id: old, weeks.id: weeks}, H100.id, "us-tx", then) == (weeks, "getdeploying-weekly")
    assert pressure.momentum_series_for({old.id: old, weeks.id: weeks}, H100.id, "us-tx", T0) == (old, "global")
    snaps = [(then, OFFERS), (T0, OFFERS)]
    history, _ = pressure.cell_history("us-tx", H100.id, snaps, gpu_models={H100.id: H100}, regions=REGIONS, series={old.id: old, weeks.id: weeks},
                                       facilities=[], markets=[], feed=[])
    earlier = pressure.compute_cell("us-tx", H100.id, offers=OFFERS, gpu_models={H100.id: H100}, regions=REGIONS, series={old.id: old, weeks.id: weeks},
                                    facilities=[], markets=[], feed=[], as_of=then)
    assert {f.factor: f for f in earlier.factors}["price-momentum"].scope == "getdeploying-weekly"
    # At `then` the weeks known end 2026-09-28 (3.0) against 2026-08-24 (3.0): flat, so momentum contributes nothing there.
    assert {f.factor: f for f in earlier.factors}["price-momentum"].raw == 0.0
    assert history[0].input_kinds == ["sample"] and len(history) == 2


def test_history_recomputes_each_snapshot_and_states_its_basis():
    # A week earlier every ask was 3.0 and available: premium 0 (flat), availability -1 -> 50 + 50 x (0.2 x -1) / 0.5 = 30
    previous = [offer(1, 3.0, "us-tx"), offer(2, 3.0, "us-tx"), offer(3, 3.0, "us-va"), offer(4, 3.0, "us-va")]
    snaps = [(T0 - timedelta(days=7), previous), (T0, OFFERS)]
    history, basis = pressure.cell_history("us-tx", H100.id, snaps, gpu_models={H100.id: H100}, regions=REGIONS, series={}, facilities=[], markets=[], feed=[])
    assert [round(p.score, 2) for p in history] == [30.0, 62.22] and [p.t for p in history] == [T0 - timedelta(days=7), T0]
    assert basis == "2 sample snapshots; FLOPX-collected history starts at first deploy"
    live_snaps = snaps + [(T0 + timedelta(days=1), [offer(1, 3.0, "us-tx", prov=live("vast-ai", T0 + timedelta(days=1)))])]
    _, basis = pressure.cell_history("us-tx", H100.id, live_snaps, gpu_models={H100.id: H100}, regions=REGIONS, series={}, facilities=[], markets=[], feed=[])
    assert basis.startswith("2 sample snapshots + FLOPX-collected since 2026-11-07")
