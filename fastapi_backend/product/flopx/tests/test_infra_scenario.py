"""Infrastructure scenarios (architecture §10.4) over the sample data: conditional, labelled, both paths."""

from datetime import datetime, timezone

import pytest

from product.flopx import scenario
from product.flopx.data.loader import load_sample_uncached

AS_OF = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def data():
    return load_sample_uncached()


def run(data, assumptions, horizon=30, region="us-tx", gpu="h100-sxm-80gb"):
    return scenario.project_infra(region, gpu, horizon, assumptions, offers=data.offers, gpu_models=data.gpu_models, regions=data.regions, series=data.series,
                                  facilities=data.facilities, markets=data.markets, feed=data.feed, companies=data.companies, as_of=AS_OF)


def test_no_assumptions_is_the_baseline_and_the_payload_is_labelled(data):
    out = run(data, [])
    assert out.statement_class == "forecast" and out.conditional is True and out.label == "demonstrative"
    assert out.scenario_score == out.baseline_score and out.pressure_delta == 0 and out.price_effect_pct == 0
    assert len(out.path) == 30 and all(p.conditional == p.baseline for p in out.path) and all(p.lower <= p.conditional <= p.upper for p in out.path)
    assert "demonstrative" in out.caveats and "sample-data" in out.caveats


def test_a_delay_counts_as_lateness_and_lowers_effective_supply_which_raises_pressure(data):
    out = run(data, [{"kind": "delay-facility", "facilityId": "fac-002", "months": 18}])
    assert out.applied == ["delay-facility:fac-002+18m"] and out.ignored == []
    delayed = next(f for f in out.scenario_supply.facilities if f.facility_id == "fac-002")
    assert delayed.completion_probability == pytest.approx((1 - 18 / 24) * 0.8)
    assert out.scenario_supply.effective_mw < out.baseline_supply.effective_mw
    assert out.pressure_delta > 0 and out.price_effect_pct > 0
    assert out.path[-1].conditional > out.path[-1].baseline and out.path[-1].upper - out.path[-1].lower > out.path[-1].baseline_upper - out.path[-1].baseline_lower
    assert out.exposure and all(m.estimated_move_pct == pytest.approx(out.price_effect_pct * m.sensitivity, abs=1e-3) for m in out.exposure)
    # Over the sample set every link rests on an authored facility or profile, so none reads documented (V1 F7):
    assert not [m for m in out.exposure if m.documented]


def test_completing_a_facility_within_the_horizon_adds_supply_and_relieves_pressure(data):
    out = run(data, [{"kind": "complete-facility", "facilityId": "fac-015", "atDate": "2026-11-20"}])
    completed = next(f for f in out.scenario_supply.facilities if f.facility_id == "fac-015")
    assert completed.completion_probability == 1.0 and completed.contributes_mw == completed.capacity_mw
    assert out.scenario_supply.effective_mw > out.baseline_supply.effective_mw and out.pressure_delta < 0
    beyond = run(data, [{"kind": "complete-facility", "facilityId": "fac-015", "atDate": "2029-01-01"}])
    assert beyond.scenario_supply.effective_mw == beyond.baseline_supply.effective_mw


def test_energy_demand_and_utilisation_assumptions_move_their_own_factor(data):
    energy = run(data, [{"kind": "energy-shock", "pct": 40}])
    assert energy.factor_deltas["power-stress"] > 0 and energy.factor_deltas["price-premium"] == 0 and energy.scenario_supply.effective_mw == energy.baseline_supply.effective_mw
    demand = run(data, [{"kind": "demand-shock", "pct": 25}])
    assert demand.factor_deltas["demand-signals"] == pytest.approx(0.1 * 0.25, abs=1e-3)
    utilisation = run(data, [{"kind": "utilisation", "value": 0.9}])
    assert utilisation.scenario_supply.effective_mw > utilisation.baseline_supply.effective_mw and utilisation.pressure_delta < 0


def test_malformed_assumptions_are_reported_not_dropped_and_bad_horizons_are_refused(data):
    out = run(data, [{"kind": "moon-phase"}, {"kind": "delay-facility", "facilityId": "fac-024", "months": 6}, {"kind": "energy-shock", "pct": 10}])
    assert [i["kind"] for i in out.ignored] == ["moon-phase", "delay-facility"] and out.applied == ["energy-shock=+10.0%"]
    assert "some-assumptions-ignored" in out.caveats
    assert all(a in out.assumptions for a in ({"kind": "moon-phase"},))
    with pytest.raises(ValueError):
        run(data, [], horizon=400)
    with pytest.raises(ValueError):
        run(data, [{"kind": "utilisation", "value": 0.99}])


def test_exposed_companies_move_by_their_own_authored_sensitivity(data):
    # V4 F5: one elasticity for every company moved all eight by the same percent.
    out = run(data, [{"kind": "delay-facility", "facilityId": "fac-002", "months": 18}])
    by = {m.ticker: m for m in out.exposure}
    expected = {"CRWV": 0.80, "NBIS": 0.80, "IREN": 0.60, "APLD": 0.55, "CORZ": 0.4125, "NVDA": 0.35, "VRT": 0.30, "SMCI": 0.30}
    for ticker, sensitivity in expected.items():
        if ticker in by:
            assert by[ticker].sensitivity == pytest.approx(sensitivity), ticker
            assert by[ticker].estimated_move_pct == pytest.approx(round(out.price_effect_pct * sensitivity, 3), abs=1e-3)
    assert len({m.estimated_move_pct for m in out.exposure}) > 1
    assert "miner-pivot" in by["IREN"].sensitivity_basis and "not a measured beta" in by["IREN"].sensitivity_basis
