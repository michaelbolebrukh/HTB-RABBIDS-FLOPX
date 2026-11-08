"""The walk-forward harness (architecture §10.6): point in time, honest verdicts, plain-Python ridge."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from product.flopx import domain, evaluation
from product.flopx.provenance import sample

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(sid, values, unit="USD"):
    return domain.Series(id=sid, unit=unit, points=[domain.PriceObservation(series_id=sid, t=T0 + timedelta(days=i), value=v, unit=unit, provenance=sample(T0 + timedelta(days=i)))
                                                   for i, v in enumerate(values)])


def test_ridge_recovers_a_linear_relationship_closely_with_a_small_penalty():
    X = [[1.0, x, y] for x, y in ((1, 2), (2, 1), (3, 5), (4, 3), (5, 8), (6, 2), (7, 7), (8, 1))]
    y = [0.5 + 2.0 * r[1] - 1.0 * r[2] for r in X]
    beta = evaluation.ridge(X, y, 0.01)
    assert beta[0] == pytest.approx(0.5, abs=0.05) and beta[1] == pytest.approx(2.0, abs=0.02) and beta[2] == pytest.approx(-1.0, abs=0.02)
    heavy = evaluation.ridge(X, y, 1000.0)
    assert abs(heavy[1]) < abs(beta[1]) and abs(heavy[2]) < abs(beta[2])      # the penalty shrinks the slopes, never the intercept's role


def test_baseline_forecast_is_the_trailing_drift_scaled_by_the_horizon():
    closes = series("equity:T:close", [100.0 * (1.01 ** i) for i in range(70)])
    b = evaluation.baseline_forecast(closes.points, 5)
    assert b["expected_log_return"] == pytest.approx(5 * math.log(1.01)) and b["lower"] == pytest.approx(b["upper"])     # zero volatility: zero band
    assert evaluation.baseline_forecast(closes.points[:60], 5) is None                                                  # under warm-up: nothing


def test_short_series_reports_insufficient_sample_for_every_horizon_rather_than_omitting_it():
    closes = series("equity:T:close", [100.0 + i for i in range(75)])
    out = evaluation.evaluate_ticker("T", closes, fcpi=None, pressure_scores=None, energy=None, feed=[])
    assert [h.horizon_days for h in out.horizons] == [1, 5, 21, 63]
    assert all(h.verdict == "insufficient-sample" for h in out.horizons)
    one_day = out.horizons[0]
    assert one_day.origins == 14 and one_day.models[0].n == 14 and one_day.models[0].verdict == "insufficient-sample"
    assert out.features_dropped == {"fcpi-change-30d": "no data for every origin", "pressure-change-30d": "no data for every origin",
                                    "energy-change-30d": "no data for every origin", "event-intensity-14d": "no variation in the window"}
    assert one_day.models[1].note == "no compute feature available" and one_day.models[2].note == "event feature has no variation in the window"
    assert out.statement_class == "inference" and "measured" not in out.reliability_note and "calibrat" not in out.reliability_note


def test_sixty_three_day_horizon_on_180_points_is_insufficient_and_never_omitted():
    closes = series("equity:T:close", [100.0 + math.sin(i / 7) for i in range(180)])
    fcpi = series("fcpi", [2.0 + math.cos(i / 9) for i in range(180)], unit="USD/GPU-hour")
    out = evaluation.evaluate_ticker("T", closes, fcpi=fcpi, pressure_scores=None, energy=None, feed=[], horizons=(63,))
    h = out.horizons[0]
    assert h.verdict == "insufficient-sample" and h.models[0].n == 57 and h.models[1].n == 0 and h.models[1].verdict == "insufficient-sample"
    assert out.features_available == ["fcpi-change-30d"] and set(out.features_dropped) == {"pressure-change-30d", "energy-change-30d", "event-intensity-14d"}
    assert h.notes[0] == "0 augmented forecasts at 63d (57 baseline origins); 20 needed"


def test_no_look_ahead_a_future_spike_in_a_feature_leaves_earlier_forecasts_unchanged():
    closes = series("equity:T:close", [100.0 + 3 * math.sin(i / 5) + i / 20 for i in range(200)])
    base_feature = [2.0 + math.cos(i / 6) for i in range(200)]
    spiked = base_feature[:-1] + [50.0]
    plain = evaluation.evaluate_ticker("T", closes, fcpi=series("fcpi", base_feature), pressure_scores=None, energy=None, feed=[], horizons=(5,))
    spike = evaluation.evaluate_ticker("T", closes, fcpi=series("fcpi", spiked), pressure_scores=None, energy=None, feed=[], horizons=(5,))
    before = [r for r in plain.horizons[0].models[1].realised_vs_predicted if r["t"] < closes.points[-1].t - timedelta(days=5)]
    after = [r for r in spike.horizons[0].models[1].realised_vs_predicted if r["t"] < closes.points[-1].t - timedelta(days=5)]
    assert before and before == after


def _baseline_errors(error: float, days: int = 50) -> dict:
    return {T0 + timedelta(days=i): error for i in range(days)}


def test_verdict_thresholds_and_metric_shapes():
    # realised +-0.01, predicted +-0.02 with the right sign: every error is 0.01, every sign hits, every realised sits in the band
    rows = [(T0 + timedelta(days=i), 0.01 * (1 if i % 2 else -1), 0.02 * (1 if i % 2 else -1), -0.02, 0.02) for i in range(50)]
    perfect = evaluation._metrics("augmented", rows, _baseline_errors(0.02), ["fcpi-change-30d"])
    assert perfect.rmse == 0.01 and perfect.skill_vs_baseline == pytest.approx(0.5) and perfect.hit_rate == 1.0 and perfect.coverage80 == 1.0 and perfect.verdict == "improves"
    assert perfect.baseline_rmse_matched == 0.02 and perfect.realised_vs_predicted_truncated_from is None and len(perfect.realised_vs_predicted) == 50
    worse = evaluation._metrics("augmented", rows, _baseline_errors(0.005), [])
    assert worse.verdict == "worsens"
    thin = evaluation._metrics("augmented", rows[:19], _baseline_errors(0.02), [])
    assert thin.verdict == "insufficient-sample" and thin.n == 19
    small_gain = evaluation._metrics("augmented", rows[:30], _baseline_errors(0.0103), [])
    assert small_gain.verdict == "inconclusive"
    assert evaluation._metrics("baseline", [], None, []).verdict == "insufficient-sample"


def test_skill_compares_the_baseline_on_exactly_the_origins_the_model_scored():
    # Hand computation. Baseline errors: 0.03 on days 0-1, 0.01 on days 2-3, 0.04 on day 4 (five origins).
    # The model scored days 2-4 only, with errors 0.02, 0.02, 0.02: its RMSE is 0.02.
    # Matched baseline RMSE over days 2-4 = sqrt((0.01^2 + 0.01^2 + 0.04^2) / 3) = sqrt(0.0018 / 3) = 0.0244949.
    # Matched skill = 1 - 0.02 / 0.0244949 = 0.183503. The unmatched baseline RMSE over all five,
    # sqrt((0.0009 * 2 + 0.0001 * 2 + 0.0016) / 5) = sqrt(0.00072) = 0.0268328, would have said 0.254644 instead.
    errors = {T0 + timedelta(days=i): e for i, e in enumerate((0.03, 0.03, 0.01, 0.01, 0.04))}
    rows = [(T0 + timedelta(days=i), 0.05, 0.03, 0.0, 0.1) for i in (2, 3, 4)]
    result = evaluation._metrics("augmented", rows, errors, ["fcpi-change-30d"])
    assert result.rmse == pytest.approx(0.02) and result.baseline_rmse_matched == pytest.approx(0.0244949, abs=1e-6)
    assert result.skill_vs_baseline == pytest.approx(0.1835, abs=1e-4)
    with pytest.raises(ValueError):
        evaluation._metrics("augmented", [(T0 + timedelta(days=9), 0.0, 0.0, 0.0, 0.0)], errors, [])


def test_more_than_sixty_rows_say_how_many_were_cut():
    rows = [(T0 + timedelta(days=i), 0.01, 0.01, 0.0, 0.02) for i in range(75)]
    result = evaluation._metrics("augmented", rows, {r[0]: 0.02 for r in rows}, [])
    assert len(result.realised_vs_predicted) == 60 and result.realised_vs_predicted_truncated_from == 75
    assert result.realised_vs_predicted[0]["t"] == T0 + timedelta(days=15)


def test_a_feature_with_no_variation_is_dropped_and_never_produces_a_verdict_from_the_intercept_alone():
    closes = series("equity:T:close", [100.0 + 3 * math.sin(i / 5) + i / 20 for i in range(200)])
    flat = series("fcpi", [2.0] * 200, unit="USD/GPU-hour")
    out = evaluation.evaluate_ticker("T", closes, fcpi=flat, pressure_scores=None, energy=None, feed=[], horizons=(5,))
    assert out.features_dropped["fcpi-change-30d"] == "no variation in the window" and out.features_available == []
    h = out.horizons[0]
    augmented, combined = h.models[1], h.models[2]
    assert h.verdict == "insufficient-sample" and augmented.n == 0 and augmented.note == "no compute feature available"
    assert combined.n == 0 and combined.verdict == "insufficient-sample" and combined.note == "event feature has no variation in the window"
    assert "no compute feature available: augmented reports insufficient-sample rather than a verdict" in h.notes


def test_combined_forecasts_only_where_the_event_feature_varies_in_the_trailing_window():
    closes = series("equity:T:close", [100.0 + 3 * math.sin(i / 5) + i / 20 for i in range(200)])
    fcpi = series("fcpi", [2.0 + math.cos(i / 6) for i in range(200)], unit="USD/GPU-hour")
    # One verified item on day 150: the event feature is non-zero on days 150-164 only, so a trailing training
    # window (origins o with o + 5 <= t) first varies once origin 150 is in it, at t >= 155.
    at = T0 + timedelta(days=150)
    item = domain.IntelItem(id="evt", kind="announcement", headline="h", why_it_matters="", published_at=at, observed_at=at, severity=5,
                            classification="verified", provenance=sample(at), entities=domain.IntelEntities(tickers=["T"]))
    out = evaluation.evaluate_ticker("T", closes, fcpi=fcpi, pressure_scores=None, energy=None, feed=[item], horizons=(5,))
    h = out.horizons[0]
    combined = h.models[2]
    first = min(r["t"] for r in combined.realised_vs_predicted)
    assert first == T0 + timedelta(days=155) and combined.features_used == ["fcpi-change-30d", "event-intensity-14d"]
    # Origins 155..194 (the last with a 5-day outcome) inside the 60-origin window: 40 combined forecasts.
    assert combined.n == 40 and "event-intensity-14d" not in out.features_dropped
