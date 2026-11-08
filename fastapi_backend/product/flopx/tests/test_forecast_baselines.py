"""Forecasting baselines on synthetic series with known answers, and the refusals."""

import math

import pytest

from product.exceptions import InsufficientHistoryError
from product.flopx import forecast as fc


def test_constant_series_every_baseline_returns_the_constant_with_zero_width():
    y = [5.0] * 40
    result = fc.forecast(y, 7, season=7)
    for b in result.baselines:
        assert b.mean == [5.0] * 7, b.method
        assert b.residual_std == 0.0
        assert b.lower == b.upper == [5.0] * 7
    assert result.chosen == "naive"   # nothing can beat naive by 2% on a flat line


def test_linear_series_drift_is_exact_and_naive_is_flat():
    y = [10.0 + 2.0 * k for k in range(30)]        # slope 2, last 68
    result = fc.forecast(y, 5, season=7)
    by = {b.method: b for b in result.baselines}
    assert by["drift"].mean == pytest.approx([70.0, 72.0, 74.0, 76.0, 78.0])
    assert by["drift"].parameters["slope"] == pytest.approx(2.0)
    assert by["naive"].mean == [68.0] * 5
    # Drift forecasts a linear series perfectly; naive is off by 2k:
    scores = {s.method: s for s in result.backtest}
    assert scores["drift"].mae == pytest.approx(0.0, abs=1e-9)
    assert scores["naive"].mae == pytest.approx(6.0)        # mean of |2|,|4|,|6|,|8|,|10|
    assert scores["drift"].mae_vs_naive == pytest.approx(0.0, abs=1e-9)
    assert result.chosen == "drift"


def test_seasonal_pattern_is_reproduced_exactly_by_seasonal_naive():
    pattern = [1.0, 3.0, 2.0, 5.0, 4.0, 6.0, 0.5]
    y = pattern * 6
    result = fc.forecast(y, 7, season=7)
    by = {b.method: b for b in result.baselines}
    assert by["seasonal-naive"].mean == pattern
    assert by["seasonal-naive"].residual_std == 0.0
    assert result.chosen == "seasonal-naive"


def test_intervals_widen_with_the_horizon_and_match_the_requested_level():
    y = [100.0, 102.0, 99.0, 103.0, 98.0, 104.0, 97.0, 105.0, 96.0, 106.0, 95.0, 107.0]
    narrow = fc.forecast(y, 4, level=0.5)
    wide = fc.forecast(y, 4, level=0.95)
    n_naive = next(b for b in narrow.baselines if b.method == "naive")
    w_naive = next(b for b in wide.baselines if b.method == "naive")
    widths_n = [u - l for l, u in zip(n_naive.lower, n_naive.upper)]
    widths_w = [u - l for l, u in zip(w_naive.lower, w_naive.upper)]
    assert widths_n == sorted(widths_n) and widths_n[0] < widths_n[-1]
    assert all(w > n for n, w in zip(widths_n, widths_w))
    # Random-walk widening: width_h = width_1 * sqrt(h)
    assert widths_n[3] == pytest.approx(widths_n[0] * math.sqrt(4))


def test_ses_alpha_is_fitted_and_level_follows_the_last_values():
    y = [1.0] * 20 + [10.0] * 20
    level, alpha = fc.ses_fit(y)
    assert 0.5 < alpha <= 0.99         # a step change rewards fast adaptation
    assert level == pytest.approx(10.0, abs=0.05)


def test_short_history_is_refused_with_the_numbers():
    with pytest.raises(InsufficientHistoryError) as exc:
        fc.forecast([1.0, 2.0, 3.0] * 6 + [1.0, 2.0], 14)
    assert exc.value.details == {
        "reason": "history-shorter-than-twice-horizon", "history_points": 20, "horizon": 14, "required_points": 28,
    }
    with pytest.raises(InsufficientHistoryError):
        fc.forecast([1.0, 2.0, 3.0], 0)


def test_seasonal_baseline_is_dropped_with_a_reason_when_fewer_than_two_seasons_exist():
    y = [float(k) for k in range(10)]
    result = fc.forecast(y, 3, season=7)
    assert [b.method for b in result.baselines] == ["naive", "drift", "ses"]
    assert "seasonal-naive-dropped:history<14" in result.caveats
