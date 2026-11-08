"""Baseline forecasts with honest intervals and an out-of-sample comparison.

Four baselines (naive, drift, seasonal-naive, simple exponential smoothing),
each with prediction intervals built from the in-sample one-step residual
spread and widened with the horizon by the textbook factors (Hyndman &
Athanasopoulos, FPP3 §5.5: naive sqrt(h), drift sqrt(h(1+h/T)), seasonal naive
sqrt(k+1) with k = floor((h-1)/m), SES sqrt(1+(h-1)alpha^2)). A rolling-origin
backtest scores every method by MAE and MAPE and reports MAE relative to naive,
so the "chosen" baseline is the one that beat naive out of sample, or naive.

Refusals are part of the contract: a horizon needing more history than exists
(history < 2 x horizon) raises InsufficientHistoryError with the numbers, and a
seasonal baseline is dropped (with a reason) when fewer than two seasons exist.
No learned model is claimed to have predictive power.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from product.exceptions import InsufficientHistoryError

METHODS = ("naive", "drift", "seasonal-naive", "ses")
MIN_HISTORY_MULTIPLE = 2
MIN_BACKTEST_ORIGINS = 3


@dataclass(frozen=True)
class Baseline:
    method: str
    mean: list[float]
    lower: list[float]
    upper: list[float]
    residual_std: float
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Score:
    method: str
    mae: float
    mape: float
    mae_vs_naive: float
    origins: int


@dataclass(frozen=True)
class ForecastResult:
    horizon: int
    level: float
    history_points: int
    baselines: list[Baseline]
    backtest: list[Score]
    chosen: str
    caveats: list[str]


# -- point forecasts -------------------------------------------------------------

def naive(y: list[float], h: int) -> list[float]:
    return [y[-1]] * h


def drift(y: list[float], h: int) -> list[float]:
    if len(y) < 2:
        return naive(y, h)
    slope = (y[-1] - y[0]) / (len(y) - 1)
    return [y[-1] + slope * k for k in range(1, h + 1)]


def seasonal_naive(y: list[float], h: int, m: int) -> list[float]:
    n = len(y)
    return [y[n - m + ((k - 1) % m)] for k in range(1, h + 1)]


def ses_fit(y: list[float], alpha: float | None = None) -> tuple[float, float]:
    """Simple exponential smoothing level; alpha by grid search on one-step SSE when not given."""
    def run(a: float) -> tuple[float, float]:
        level = y[0]
        sse = 0.0
        for value in y[1:]:
            sse += (value - level) ** 2
            level = level + a * (value - level)
        return level, sse

    if alpha is not None:
        return run(alpha)[0], alpha
    best_alpha, best_sse, best_level = 0.1, math.inf, y[-1]
    for step in range(1, 100):
        a = step / 100.0
        level, sse = run(a)
        if sse < best_sse:
            best_alpha, best_sse, best_level = a, sse, level
    return best_level, best_alpha


def ses(y: list[float], h: int, alpha: float | None = None) -> tuple[list[float], float]:
    level, a = ses_fit(y, alpha)
    return [level] * h, a


def point_forecast(method: str, y: list[float], h: int, m: int) -> tuple[list[float], dict]:
    if method == "naive":
        return naive(y, h), {}
    if method == "drift":
        return drift(y, h), {"slope": (y[-1] - y[0]) / (len(y) - 1) if len(y) > 1 else 0.0}
    if method == "seasonal-naive":
        return seasonal_naive(y, h, m), {"season": m}
    if method == "ses":
        mean, a = ses(y, h)
        return mean, {"alpha": a}
    raise ValueError(method)


# -- residuals and intervals ---------------------------------------------------

def one_step_residuals(method: str, y: list[float], m: int) -> list[float]:
    """In-sample one-step-ahead errors, the empirical basis of every interval."""
    residuals: list[float] = []
    start = m if method == "seasonal-naive" else 2
    for i in range(start, len(y)):
        history = y[:i]
        forecast, _ = point_forecast(method, history, 1, m)
        residuals.append(y[i] - forecast[0])
    return residuals


def _z(level: float) -> float:
    return statistics.NormalDist().inv_cdf(0.5 + level / 2.0)


def _widening(method: str, h: int, n: int, m: int, params: dict) -> float:
    if method == "naive":
        return math.sqrt(h)
    if method == "drift":
        return math.sqrt(h * (1 + h / max(n, 1)))
    if method == "seasonal-naive":
        return math.sqrt(((h - 1) // m) + 1)
    if method == "ses":
        a = params.get("alpha", 0.5)
        return math.sqrt(1 + (h - 1) * a * a)
    return math.sqrt(h)


def baseline(method: str, y: list[float], h: int, m: int, level: float) -> Baseline:
    mean, params = point_forecast(method, y, h, m)
    residuals = one_step_residuals(method, y, m)
    std = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    z = _z(level)
    lower, upper = [], []
    for k, value in enumerate(mean, start=1):
        half = z * std * _widening(method, k, len(y), m, params)
        lower.append(value - half)
        upper.append(value + half)
    return Baseline(method=method, mean=mean, lower=lower, upper=upper, residual_std=std, parameters=params)


# -- backtest --------------------------------------------------------------------

def rolling_origin_backtest(y: list[float], h: int, m: int, methods: tuple[str, ...]) -> list[Score]:
    """Forecast h steps from every origin that leaves h points to score against."""
    n = len(y)
    min_train = max(2 * m if "seasonal-naive" in methods else 2, h, 5)
    origins = list(range(min_train, n - h + 1))
    if len(origins) < MIN_BACKTEST_ORIGINS:
        return []
    errors: dict[str, list[float]] = {method: [] for method in methods}
    pct_errors: dict[str, list[float]] = {method: [] for method in methods}
    for origin in origins:
        train, test = y[:origin], y[origin:origin + h]
        for method in methods:
            forecast, _ = point_forecast(method, train, h, m)
            for actual, predicted in zip(test, forecast):
                errors[method].append(abs(actual - predicted))
                if actual != 0:
                    pct_errors[method].append(abs(actual - predicted) / abs(actual))
    naive_mae = statistics.fmean(errors["naive"]) if errors.get("naive") else None
    scores = []
    for method in methods:
        mae = statistics.fmean(errors[method])
        mape = statistics.fmean(pct_errors[method]) * 100.0 if pct_errors[method] else math.nan
        scores.append(Score(
            method=method, mae=mae, mape=mape,
            mae_vs_naive=(mae / naive_mae) if naive_mae else 1.0,
            origins=len(origins),
        ))
    return scores


# -- the entry point -------------------------------------------------------------

def forecast(y: list[float], horizon: int, *, level: float = 0.8, season: int = 7) -> ForecastResult:
    n = len(y)
    if horizon < 1:
        raise InsufficientHistoryError(reason="horizon-below-one", horizon=horizon)
    if n < MIN_HISTORY_MULTIPLE * horizon or n < 3:
        raise InsufficientHistoryError(
            reason="history-shorter-than-twice-horizon",
            history_points=n, horizon=horizon, required_points=max(MIN_HISTORY_MULTIPLE * horizon, 3),
        )

    caveats = ["baselines-only", "intervals-from-in-sample-residuals"]
    methods = ["naive", "drift", "ses"]
    if n >= 2 * season:
        methods.insert(2, "seasonal-naive")
    else:
        caveats.append(f"seasonal-naive-dropped:history<{2 * season}")

    baselines = [baseline(method, y, horizon, season, level) for method in methods]
    scores = rolling_origin_backtest(y, horizon, season, tuple(methods))
    if scores:
        chosen = min(scores, key=lambda s: s.mae).method
        if chosen != "naive" and all(s.mae_vs_naive >= 0.98 for s in scores if s.method == chosen):
            chosen = "naive"
            caveats.append("no-baseline-beat-naive-by-2pct")
    else:
        chosen = "naive"
        caveats.append("backtest-skipped:too-few-origins")
    if n < 4 * horizon:
        caveats.append("thin-history-relative-to-horizon")
    return ForecastResult(
        horizon=horizon, level=level, history_points=n,
        baselines=baselines, backtest=scores, chosen=chosen, caveats=caveats,
    )
