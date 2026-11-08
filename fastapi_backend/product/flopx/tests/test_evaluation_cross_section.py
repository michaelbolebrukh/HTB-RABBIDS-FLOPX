"""IC and the sign-of-forecast Sharpe across tickers, and the history coverage: research numbers with known answers."""

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from product.flopx import domain, evaluation
from product.flopx.provenance import sample

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def series(sid, values, unit="USD"):
    return domain.Series(id=sid, unit=unit, points=[domain.PriceObservation(series_id=sid, t=T0 + i * DAY, value=v, unit=unit, provenance=sample(T0 + i * DAY))
                                                   for i, v in enumerate(values)])


def row(day: int, h: int, realised: float, predicted: float):
    return (T0 + day * DAY, T0 + (day + h) * DAY, realised, predicted)


# -- Spearman ------------------------------------------------------------------------------------

def test_spearman_is_one_for_any_monotone_map_minus_one_when_reversed_and_none_when_a_side_is_constant():
    assert evaluation.spearman([1.0, 2.0, 3.0, 4.0], [10.0, 100.0, 1000.0, 10000.0]) == pytest.approx(1.0)
    assert evaluation.spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert evaluation.spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert evaluation.spearman([1.0], [2.0]) is None


def test_spearman_shares_average_ranks_on_ties():
    # x ranks: 1, 2.5, 2.5, 4; y ranks: 1, 2, 3, 4 -> Pearson of the ranks, computed by hand: 0.9487
    assert evaluation.spearman([1.0, 2.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(0.9487, abs=1e-4)


# -- IC across tickers ---------------------------------------------------------------------------

def test_cross_section_mean_ic_and_t_stat_on_a_panel_with_a_known_answer():
    # Five tickers, four dates. On dates 0 and 1 the forecast ranks the realised returns perfectly (IC = 1); on
    # dates 2 and 3 it ranks them exactly backwards (IC = -1): mean IC 0, t-stat 0. A fifth date has two tickers
    # only and is not scored; the share of dates with five or more tickers is 1.0 over the four scored.
    h = 5
    tickers = ["A", "B", "C", "D", "E"]
    panels = {t: {(h, "augmented"): []} for t in tickers}
    for d in range(4):
        for k, t in enumerate(tickers):
            realised = 0.01 * (k + 1)
            predicted = realised if d < 2 else -realised
            panels[t][(h, "augmented")].append(row(d, h, realised, predicted))
    for t in tickers[:2]:
        panels[t][(h, "augmented")].append(row(9, h, 0.02, 0.01))
    [res] = evaluation.cross_section(panels, horizons=(h,), models=("augmented",))
    assert res.n_dates == 4 and res.mean_ic == pytest.approx(0.0) and res.t_stat == pytest.approx(0.0)
    assert res.share_dates_five_plus == 1.0 and res.tickers == 5 and res.provenance == "research"
    assert "optimistic" in res.note


def test_cross_section_positive_ic_has_a_positive_t_stat_and_counts_narrow_dates_in_the_share():
    h = 1
    tickers = ["A", "B", "C", "D"]
    panels = {t: {(h, "baseline"): []} for t in tickers}
    # Three dates: perfect ranking twice (IC 1), one date where the two middle names swap (IC 0.8).
    for d, swap in enumerate((False, False, True)):
        order = [0, 2, 1, 3] if swap else [0, 1, 2, 3]
        for k, t in enumerate(tickers):
            panels[t][(h, "baseline")].append(row(d, h, 0.01 * (k + 1), 0.01 * (order[k] + 1)))
    [res] = evaluation.cross_section(panels, horizons=(h,), models=("baseline",))
    assert res.n_dates == 3 and res.mean_ic == pytest.approx((1 + 1 + 0.8) / 3, abs=1e-4)
    ics = [1.0, 1.0, 0.8]
    mean = sum(ics) / 3
    sd = math.sqrt(sum((ic - mean) ** 2 for ic in ics) / 2)
    assert res.t_stat == pytest.approx(mean / (sd / math.sqrt(3)), abs=1e-2)       # 14.0 by hand
    assert res.share_dates_five_plus == 0.0       # four tickers a date, never five


def test_cross_section_reports_zero_dates_rather_than_omitting_a_model_nobody_scored():
    panels = {"A": {(5, "baseline"): [row(0, 5, 0.01, 0.02)]}, "B": {(5, "baseline"): [row(0, 5, 0.02, 0.01)]}}
    [res] = evaluation.cross_section(panels, horizons=(5,), models=("combined",))
    assert res.n_dates == 0 and res.mean_ic is None and res.t_stat is None and res.share_dates_five_plus is None and res.tickers == 0


# -- the sign-of-forecast Sharpe -------------------------------------------------------------------

def test_tradable_takes_non_overlapping_holds_and_the_sharpe_matches_the_hand_calculation():
    # One ticker, h = 5, a forecast at every day for 40 days: only every fifth origin is tradable (8 trades).
    # The forecast's sign is always right and the realised h-day returns at the traded origins are the eight
    # values below, so the ticker's own drift over the traded origins, the signed returns and the excess are all
    # computable by hand: excess = side * realised - side * drift.
    h = 5
    realised_by_trade = [0.01, -0.03, 0.02, -0.01, 0.03, -0.02, 0.01, -0.04]
    rows = [row(day, h, realised_by_trade[day // 5], 0.5 * realised_by_trade[day // 5]) for day in range(40)]
    [res] = evaluation.tradable({"A": {(h, "augmented"): rows}}, horizons=(h,), models=("augmented",))
    trades = evaluation.non_overlapping(rows)
    assert [r[0] for r in trades] == [T0 + d * DAY for d in range(0, 40, 5)] and res.n_trades == 8
    drift = sum(realised_by_trade) / 8
    excess = [abs(r) - (1 if r > 0 else -1) * drift for r in realised_by_trade]
    mean = sum(excess) / 8
    sd = math.sqrt(sum((e - mean) ** 2 for e in excess) / 7)
    assert res.mean_excess_log_return == pytest.approx(mean, abs=1e-6)
    assert res.sharpe == pytest.approx(mean / sd * math.sqrt(252 / h), abs=1e-3)
    assert res.hit_rate == 1.0 and res.tickers == 1 and res.provenance == "research" and "no costs" in res.note


def test_tradable_scores_zero_for_a_forecast_that_only_rides_the_drift():
    # Always long on a steadily rising name: every trade's excess over the ticker's own mean is zero, so there is
    # no Sharpe to claim (the spread is zero), and the hit rate alone says 1.0, which is what the drift earns.
    h = 1
    rows = [row(day, h, 0.01, 0.02) for day in range(30)]
    [res] = evaluation.tradable({"A": {(h, "baseline"): rows}}, horizons=(h,), models=("baseline",))
    assert res.n_trades == 30 and res.mean_excess_log_return == pytest.approx(0.0) and res.sharpe is None and res.hit_rate == 1.0


def test_tradable_pools_tickers_and_skips_a_zero_forecast():
    h = 1
    a = [row(day, h, 0.01 * (1 if day % 2 == 0 else -1), 0.01 * (1 if day % 2 == 0 else -1)) for day in range(10)]
    b = [row(day, h, 0.02 * (1 if day % 3 == 0 else -1), 0.0) for day in range(10)]       # a flat forecast trades nothing
    [res] = evaluation.tradable({"A": {(h, "augmented"): a}, "B": {(h, "augmented"): b}}, horizons=(h,), models=("augmented",))
    assert res.n_trades == 10 and res.tickers == 1 and res.hit_rate == 1.0


# -- the panel the composition hands over, and the coverage ----------------------------------------

def test_the_panel_holds_every_scored_origin_with_its_exit_date_and_leaves_the_evaluation_unchanged():
    closes = series("equity:T:close", [100.0 + 3 * math.sin(i / 5) + i / 20 for i in range(200)])
    fcpi = series("fcpi", [2.0 + math.cos(i / 6) for i in range(200)], unit="USD/GPU-hour")
    result, panel = evaluation.evaluate_ticker_with_panel("T", closes, fcpi=fcpi, pressure_scores=None, energy=None, feed=[], horizons=(5, 21))
    plain = evaluation.evaluate_ticker("T", closes, fcpi=fcpi, pressure_scores=None, energy=None, feed=[], horizons=(5, 21))
    assert replace(result, provenance=None) == replace(plain, provenance=None)      # the provenance carries a derived-at stamp
    for h in result.horizons:
        for m in h.models:
            rows = panel[(h.horizon_days, m.model)]
            assert len(rows) == m.n
            assert all(exit_t - t == h.horizon_days * DAY for t, exit_t, _, _ in rows)
            if m.realised_vs_predicted:
                shown = m.realised_vs_predicted[-1]
                assert rows[-1][0] == shown["t"] and rows[-1][2] == pytest.approx(shown["realised"], abs=1e-6)
    assert set(panel) == {(5, "baseline"), (5, "augmented"), (5, "combined"), (21, "baseline"), (21, "augmented"), (21, "combined")}


def test_history_coverage_reads_the_closes_and_the_origins_per_horizon():
    closes = series("equity:T:close", [100.0 + i for i in range(120)])
    result = evaluation.evaluate_ticker("T", closes, fcpi=None, pressure_scores=None, energy=None, feed=[], horizons=(1, 21))
    cov = evaluation.history_coverage(result, closes)
    assert cov.ticker == "T" and cov.first_close == T0 and cov.last_close == T0 + 119 * DAY and cov.trading_days == 120
    assert cov.origins_by_horizon == {1: 59, 21: 39} and cov.origins_scored == 59 and cov.span_years == pytest.approx(119 / 365.25, abs=0.01)
    assert cov.input_kinds == ["sample"]
