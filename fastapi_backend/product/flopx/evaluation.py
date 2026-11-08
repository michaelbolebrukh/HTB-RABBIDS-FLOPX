"""Walk-forward, point-in-time forecast evaluation (architecture §10.6): research, with honest verdicts.

Over daily closes for one ticker, every origin t after a 60-day warm-up asks
three models for the log return to t+h (h in 1, 5, 21, 63 trading days) using
only data observed by t:

- `baseline`: the trailing 60-day mean daily log return times h, interval from
  the trailing volatility (z for the 80% band, scaled by sqrt(h)).
- `augmented`: baseline + beta . features_t, beta fitted by ridge regression
  (lambda = 1.0) on the trailing 60 origins whose outcome was already known at
  t (origin + h <= t), features standardised on that window: the 30-day change
  of the FCPI series for the model basket, the 30-day change of the pressure
  score for the ticker's top documented region, the 30-day change of that
  region's energy price. A feature with no data for the ticker, or no
  variation over the evaluation window, is dropped and the payload says so; a
  feature with no variation in one origin's trailing window is left out of
  that origin's fit. When the FCPI index has no history covering the window
  the composition substitutes the raw on-demand price series and the feature
  is named `gpu-price-change-30d`, so the payload never calls it the FCPI.
  An origin where no feature varies gets no augmented forecast (the fit would
  be the intercept alone), so a ticker without a compute feature reports
  `insufficient-sample` with the note "no compute feature available".
- `combined`: augmented plus an event feature, the severity-weighted count of
  feed items naming the ticker in the trailing 14 days (verified or
  interpretation only). An origin where the event feature does not vary over
  the trailing window gets no combined forecast: it would carry no information
  beyond augmented's.

Per (ticker, horizon, model): n, MAE, RMSE, sign hit rate, coverage of the 80%
band, skill = 1 - rmse_model / rmse_baseline where the baseline RMSE is taken
over exactly the origins the model scored (`baselineRmseMatched`), the
realised-vs-predicted series (the last 60 rows, with the full count in
`realisedVsPredictedTruncatedFrom` when more were scored), and a verdict:
`improves` needs skill > 0.05 and n >= 40, `worsens` skill < -0.05, otherwise
`inconclusive`; a horizon with fewer than 20 scored origins is reported as
`insufficient-sample`, never omitted. Nothing here is a calibration claim:
coverage is what the band actually caught, on the data given, and results on
sample closes say `inputKinds: [sample]`.

Ridge regression is plain Python (a small Gaussian elimination); numpy is not a
dependency of this backend and the matrices are 5 x 5 at most.

Two research numbers sit on top of the per-ticker scores, computed by the
composition over every ticker it evaluated (`cross_section`, `tradable`), with
provenance `research`: they rank and trade the forecasts, which the per-ticker
harness never does, and they change no verdict.

- IC, per (horizon, model): for each origin date on which at least
  `MIN_CROSS_SECTION` tickers hold both a forecast and a realised return, the
  Spearman rank correlation across those tickers between the forecast and the
  realised log return; reported as the number of dates, the mean IC, its
  t-statistic (mean over the standard error across dates) and the share of dates
  with at least five tickers. Origins overlap for h > 1, so the t-statistic
  treats correlated dates as independent and is optimistic; the note says so.
- Sharpe, per (horizon, model): the simplest tradable use of the forecast. Per
  ticker, long when the forecast is positive and short when negative, held h
  trading days, origins taken non-overlapping (the next trade opens at or after
  the previous exit); each trade's return is the signed realised log return less
  that ticker's mean realised return over its own traded origins, so a model
  that only rides the drift scores zero. Trades are pooled across tickers and
  Sharpe = mean / sample standard deviation times sqrt(252 / h). No costs, no
  slippage, point in time, a research number and never a traded record.
- History coverage, per ticker: first and last close, trading days, and the
  origins scored at each horizon, so "enough history?" is answered with numbers.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from product.flopx import domain
from product.flopx.provenance import Provenance, derived, effective_kind
from product.flopx.series import point_in_time

HORIZONS = (1, 5, 21, 63)
WARMUP_DAYS = 60
TRAIN_ORIGINS = 60
MIN_TRAIN_ORIGINS = 10
RIDGE_LAMBDA = 1.0
LEVEL = 0.80
Z80 = statistics.NormalDist().inv_cdf(0.5 + LEVEL / 2.0)
MIN_N = 20
IMPROVES_N = 40
SKILL_THRESHOLD = 0.05
FEATURE_DAYS = 30
EVENT_DAYS = 14
EVENT_CLASSES = ("verified", "interpretation")
MODELS = ("baseline", "augmented", "combined")
FCPI_FEATURE = "fcpi-change-30d"
GPU_PRICE_FEATURE = "gpu-price-change-30d"     # the raw on-demand series standing in for an FCPI without history
PRESSURE_FEATURE = "pressure-change-30d"
ENERGY_FEATURE = "energy-change-30d"
FEATURE_NAMES = (FCPI_FEATURE, PRESSURE_FEATURE, ENERGY_FEATURE)
PRICE_FEATURES = (FCPI_FEATURE, GPU_PRICE_FEATURE)
EVENT_FEATURE = "event-intensity-14d"
SERIES_SHOWN = 60
VARIANCE_EPSILON = 1e-12
NO_VARIATION = "no variation in the window"
NO_COMPUTE_FEATURE = "no compute feature available"
EVENT_NO_VARIATION = "event feature has no variation in the window"
MIN_CROSS_SECTION = 3           # tickers a date needs before a rank correlation across them means anything
WIDE_CROSS_SECTION = 5          # the share of dates at or above this is reported beside the mean IC
TRADING_DAYS_PER_YEAR = 252
SPREAD_EPSILON = 1e-9           # a standard deviation of trade returns below this is floating-point noise, not a spread
RESEARCH_PROVENANCE = "research"
IC_NOTE = ("Spearman rank correlation across tickers per origin date, averaged over dates; origins overlap for h > 1, "
           "so the t-statistic treats overlapping dates as independent and is optimistic")
SHARPE_NOTE = ("long when the forecast is positive and short when negative, held h trading days from non-overlapping origins, "
               "each trade's return less the ticker's own mean realised return over its traded origins, annualised by sqrt(252/h); "
               "no costs, no slippage, point in time; a research number, not a traded record")

# A scored forecast as the cross-ticker metrics read it: origin date, exit date, realised and predicted log return.
ForecastRow = tuple[datetime, datetime, float, float]
Panel = dict[tuple[int, str], list[ForecastRow]]


@dataclass(frozen=True)
class ModelResult:
    model: str
    n: int
    mae: float | None
    rmse: float | None
    hit_rate: float | None
    coverage80: float | None
    skill_vs_baseline: float | None
    verdict: str
    realised_vs_predicted: list[dict] = field(default_factory=list)
    features_used: list[str] = field(default_factory=list)
    # The baseline's RMSE over exactly the origins this model scored: skill = 1 - rmse / baseline_rmse_matched.
    baseline_rmse_matched: float | None = None
    # The full count when realised_vs_predicted holds only the last SERIES_SHOWN rows; None when nothing was cut.
    realised_vs_predicted_truncated_from: int | None = None
    # Why this model has fewer forecasts than the baseline, when it is not the plain training warm-up.
    note: str | None = None


@dataclass(frozen=True)
class HorizonResult:
    ticker: str
    horizon_days: int
    origins: int
    models: list[ModelResult]
    verdict: str
    notes: list[str]


@dataclass(frozen=True)
class TickerEvaluation:
    ticker: str
    horizons: list[HorizonResult]
    features_available: list[str]
    features_dropped: dict[str, str]
    feature_sources: dict[str, str]
    input_kinds: list[str]
    provenance: Provenance
    method: str = "walk-forward-ridge-v1"
    statement_class: str = "inference"
    reliability_note: str = (
        "out-of-sample on the data given; a verdict on sample closes tests the method on authored data, not the market"
    )


@dataclass(frozen=True)
class CrossSectionResult:
    """Mean Spearman IC across tickers per origin date, for one (horizon, model)."""
    horizon_days: int
    model: str
    n_dates: int
    mean_ic: float | None
    t_stat: float | None
    share_dates_five_plus: float | None
    tickers: int                     # distinct tickers that contributed a forecast on at least one scored date
    provenance: str = RESEARCH_PROVENANCE
    statement_class: str = "inference"
    note: str = IC_NOTE


@dataclass(frozen=True)
class TradableResult:
    """Sharpe of the sign-of-forecast trade, pooled over tickers, for one (horizon, model)."""
    horizon_days: int
    model: str
    n_trades: int
    sharpe: float | None
    mean_excess_log_return: float | None
    hit_rate: float | None           # share of trades whose signed realised return was positive
    tickers: int
    provenance: str = RESEARCH_PROVENANCE
    statement_class: str = "inference"
    note: str = SHARPE_NOTE


@dataclass(frozen=True)
class HistoryCoverage:
    """How much history one ticker's evaluation actually stood on."""
    ticker: str
    first_close: datetime | None
    last_close: datetime | None
    trading_days: int
    span_years: float | None
    origins_scored: int              # baseline origins at the shortest horizon evaluated
    origins_by_horizon: dict[int, int]
    input_kinds: list[str]


# -- linear algebra, small and plain -------------------------------------------------------

def ridge(X: list[list[float]], y: list[float], lam: float) -> list[float]:
    """beta = (X'X + lam I)^-1 X'y with the first (intercept) column unpenalised."""
    k = len(X[0])
    A = [[sum(X[r][i] * X[r][j] for r in range(len(X))) + (lam if i == j and i > 0 else 0.0) for j in range(k)] for i in range(k)]
    b = [sum(X[r][i] * y[r] for r in range(len(X))) for i in range(k)]
    # Gaussian elimination with partial pivoting:
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(A[r][col]))
        if abs(A[pivot][col]) < 1e-12:
            continue
        A[col], A[pivot] = A[pivot], A[col]
        b[col], b[pivot] = b[pivot], b[col]
        for r in range(k):
            if r == col:
                continue
            factor = A[r][col] / A[col][col]
            if factor:
                A[r] = [a - factor * c for a, c in zip(A[r], A[col])]
                b[r] -= factor * b[col]
    return [b[i] / A[i][i] if abs(A[i][i]) > 1e-12 else 0.0 for i in range(k)]


def standardise(columns: list[list[float]]) -> tuple[list[tuple[float, float]], list[list[float]]]:
    params = []
    out = []
    for col in columns:
        mean = statistics.fmean(col)
        std = statistics.pstdev(col)
        params.append((mean, std))
        out.append([(v - mean) / std if std > 0 else 0.0 for v in col])
    return params, out


# -- features known at t ------------------------------------------------------------------

def _value_at(series: domain.Series | None, at: datetime) -> float | None:
    if series is None:
        return None
    points = point_in_time(series, at)
    return points[-1].value if points else None


def change_over(series: domain.Series | None, at: datetime, days: int) -> float | None:
    now = _value_at(series, at)
    then = _value_at(series, at - timedelta(days=days))
    if now is None or then is None or then == 0:
        return None
    return now / then - 1.0


def score_change_over(series: domain.Series | None, at: datetime, days: int) -> float | None:
    """A difference, not a ratio: pressure scores are points on 0-100."""
    now = _value_at(series, at)
    then = _value_at(series, at - timedelta(days=days))
    if now is None or then is None:
        return None
    return (now - then) / 100.0


def event_intensity(feed: list[domain.IntelItem], ticker: str, at: datetime, days: int = EVENT_DAYS) -> float:
    since = at - timedelta(days=days)
    return sum(i.severity / 5.0 for i in feed if ticker in i.entities.tickers and i.classification in EVENT_CLASSES and since <= i.observed_at <= at)


def baseline_forecast(closes: list[domain.PriceObservation], horizon: int, window: int = WARMUP_DAYS) -> dict | None:
    """Trailing-mean drift of log returns times h, with the 80% band from trailing volatility; None under warm-up."""
    if len(closes) < window + 1:
        return None
    values = [p.value for p in closes[-(window + 1):]]
    returns = [math.log(b / a) for a, b in zip(values, values[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    mean, std = statistics.fmean(returns), statistics.pstdev(returns)
    half = Z80 * std * math.sqrt(horizon)
    return {"expected_log_return": mean * horizon, "lower": mean * horizon - half, "upper": mean * horizon + half,
            "level": LEVEL, "horizon_days": horizon, "window_days": window, "model": "baseline"}


# -- the harness ------------------------------------------------------------------------------

def covers_window(series: domain.Series | None, closes: list[domain.PriceObservation]) -> bool:
    """True when `series` has a value FEATURE_DAYS before the first origin, so its 30-day change exists at every origin."""
    if series is None or not series.points or len(closes) <= WARMUP_DAYS:
        return False
    return series.points[0].t <= closes[WARMUP_DAYS].t - timedelta(days=FEATURE_DAYS)


def select_price_feature(index: domain.Series | None, raw: domain.Series | None, closes: list[domain.PriceObservation]) -> tuple[domain.Series | None, str, str]:
    """The compute-price feature for an evaluation: (series, feature name, source text).

    The FCPI index when its history covers the window; otherwise the raw
    on-demand series it is built from, named for what it is
    (`gpu-price-change-30d`) with the substitution stated in the source text.
    Both tiers call this rule, so a one-point live index never silently
    replaces a covering history in one tier and not the other.
    """
    if covers_window(index, closes):
        return index, FCPI_FEATURE, index.id
    if raw is not None and raw.points:
        reason = "the FCPI index has no history covering the window" if index is not None and index.points else "no FCPI index series"
        return raw, GPU_PRICE_FEATURE, f"{raw.id} (raw on-demand series in place of the FCPI: {reason})"
    return None, FCPI_FEATURE, "none"


def _spread(values: list[float]) -> float:
    """Population standard deviation; zero for fewer than two values (the TypeScript port mirrors it)."""
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _feature_columns(closes: list[domain.PriceObservation], origins: list[int], feature_series: dict[str, domain.Series | None],
                     names: tuple[str, ...]) -> tuple[dict[str, list[float | None]], dict[str, str]]:
    """One column per feature over every origin; a column missing anywhere or constant everywhere is dropped."""
    price, pressure_name, energy_name = names
    cols: dict[str, list[float | None]] = {name: [] for name in names}
    for i in origins:
        at = closes[i].t
        cols[price].append(change_over(feature_series.get("fcpi"), at, FEATURE_DAYS))
        cols[pressure_name].append(score_change_over(feature_series.get("pressure"), at, FEATURE_DAYS))
        cols[energy_name].append(change_over(feature_series.get("energy"), at, FEATURE_DAYS))
    dropped = {}
    for name in names:
        if any(v is None for v in cols[name]) or not cols[name]:
            dropped[name] = "no data for every origin" if cols[name] else "no origins"
        elif _spread(cols[name]) <= VARIANCE_EPSILON:
            dropped[name] = NO_VARIATION
    return cols, dropped


def _metrics(model: str, rows: list[tuple[datetime, float, float, float, float]], baseline_errors: dict[datetime, float] | None,
             features: list[str], note: str | None = None) -> ModelResult:
    """rows: (t, realised, predicted, lower, upper); baseline_errors: the baseline's (realised - predicted) by origin.

    Skill compares like with like: the baseline RMSE is taken over exactly the
    origins in `rows`, never over the baseline's own (larger) set, so a model
    scored on fewer origins is not flattered or penalised by the ones it missed.
    """
    n = len(rows)
    if n == 0:
        return ModelResult(model, 0, None, None, None, None, None, "insufficient-sample", [], features, note=note)
    errors = [r[1] - r[2] for r in rows]
    mae = statistics.fmean(abs(e) for e in errors)
    rmse = math.sqrt(statistics.fmean(e * e for e in errors))
    hits = sum(1 for r in rows if r[1] != 0 and (r[1] > 0) == (r[2] > 0)) / n
    coverage = sum(1 for r in rows if r[3] <= r[1] <= r[4]) / n
    matched = None
    if model != "baseline" and baseline_errors:
        missing = [r[0] for r in rows if r[0] not in baseline_errors]
        if missing:
            raise ValueError(f"{model} scored {len(missing)} origins the baseline did not")
        matched = math.sqrt(statistics.fmean(baseline_errors[r[0]] ** 2 for r in rows))
    skill = (1.0 - rmse / matched) if matched else (0.0 if model == "baseline" else None)
    if n < MIN_N:
        verdict = "insufficient-sample"
    elif model == "baseline":
        verdict = "reference"
    elif skill is not None and skill > SKILL_THRESHOLD and n >= IMPROVES_N:
        verdict = "improves"
    elif skill is not None and skill < -SKILL_THRESHOLD:
        verdict = "worsens"
    else:
        verdict = "inconclusive"
    shown = [{"t": r[0], "realised": round(r[1], 6), "predicted": round(r[2], 6), "lower": round(r[3], 6), "upper": round(r[4], 6)} for r in rows[-SERIES_SHOWN:]]
    return ModelResult(model, n, round(mae, 6), round(rmse, 6), round(hits, 4), round(coverage, 4),
                       round(skill, 4) if skill is not None else None, verdict, shown, features,
                       baseline_rmse_matched=round(matched, 6) if matched is not None else None,
                       realised_vs_predicted_truncated_from=n if n > SERIES_SHOWN else None, note=note)


def evaluate_ticker(
    ticker: str,
    closes: domain.Series,
    *,
    fcpi: domain.Series | None,
    pressure_scores: domain.Series | None,
    energy: domain.Series | None,
    feed: list[domain.IntelItem],
    horizons: tuple[int, ...] = HORIZONS,
    feature_sources: dict[str, str] | None = None,
    price_feature: str = FCPI_FEATURE,
) -> TickerEvaluation:
    """`fcpi` is the compute-price series; `price_feature` names it (`select_price_feature` decides both)."""
    return evaluate_ticker_with_panel(ticker, closes, fcpi=fcpi, pressure_scores=pressure_scores, energy=energy, feed=feed,
                                      horizons=horizons, feature_sources=feature_sources, price_feature=price_feature)[0]


def evaluate_ticker_with_panel(
    ticker: str,
    closes: domain.Series,
    *,
    fcpi: domain.Series | None,
    pressure_scores: domain.Series | None,
    energy: domain.Series | None,
    feed: list[domain.IntelItem],
    horizons: tuple[int, ...] = HORIZONS,
    feature_sources: dict[str, str] | None = None,
    price_feature: str = FCPI_FEATURE,
) -> tuple[TickerEvaluation, Panel]:
    """`evaluate_ticker` plus every scored forecast, by (horizon, model), for the cross-ticker metrics.

    The panel holds all origins (the evaluation's own series keeps the last
    SERIES_SHOWN rows) with each origin's exit date, so `tradable` can take
    non-overlapping holds and `cross_section` can align tickers by date.
    """
    if price_feature not in PRICE_FEATURES:
        raise ValueError(f"unknown price feature {price_feature!r}")
    names = (price_feature, PRESSURE_FEATURE, ENERGY_FEATURE)
    points = closes.points
    log_prices = [math.log(p.value) if p.value > 0 else None for p in points]
    all_origins = [i for i in range(WARMUP_DAYS, len(points)) if log_prices[i] is not None]
    feature_series = {"fcpi": fcpi, "pressure": pressure_scores, "energy": energy}
    columns, dropped = _feature_columns(points, all_origins, feature_series, names)
    kept = [name for name in names if name not in dropped]
    origin_index = {i: k for k, i in enumerate(all_origins)}
    events = [event_intensity(feed, ticker, points[i].t) for i in all_origins]
    if not all_origins:
        dropped[EVENT_FEATURE] = "no origins"
    elif _spread(events) <= VARIANCE_EPSILON:
        dropped[EVENT_FEATURE] = NO_VARIATION
    event_kept = EVENT_FEATURE not in dropped

    def value(name: str, o: int) -> float:
        k = origin_index[o]
        return events[k] if name == EVENT_FEATURE else columns[name][k]

    results: list[HorizonResult] = []
    panel: Panel = {}
    for h in horizons:
        notes: list[str] = []
        scored = [i for i in all_origins if i + h < len(points) and log_prices[i + h] is not None]
        exits = {points[i].t: points[i + h].t for i in scored}
        baselines: dict[int, dict] = {}
        for i in all_origins:
            b = baseline_forecast(points[: i + 1], h)
            if b is not None:
                baselines[i] = b
        rows = {m: [] for m in MODELS}
        used = {"augmented": set(), "combined": set()}
        flat = {"augmented": 0, "combined": 0}     # trained origins with nothing informative to fit
        trained = 0
        for i in scored:
            b = baselines.get(i)
            if b is None:
                continue
            realised = log_prices[i + h] - log_prices[i]
            t = points[i].t
            half = (b["upper"] - b["lower"]) / 2.0
            rows["baseline"].append((t, realised, b["expected_log_return"], b["lower"], b["upper"]))
            # Training origins: the trailing window whose outcomes were known at t (o + h <= i):
            train = [o for o in all_origins if o + h <= i and o in baselines][-TRAIN_ORIGINS:]
            if len(train) < MIN_TRAIN_ORIGINS:
                continue
            trained += 1
            # Only features that vary over this trailing window enter the fit; a constant one carries nothing:
            informative = [name for name in kept if _spread([value(name, o) for o in train]) > VARIANCE_EPSILON]
            event_varies = event_kept and _spread([value(EVENT_FEATURE, o) for o in train]) > VARIANCE_EPSILON
            for model in ("augmented", "combined"):
                if model == "augmented":
                    fit_names = informative
                else:
                    # Without event variation the combined model is augmented again: no forecast of its own here.
                    fit_names = informative + [EVENT_FEATURE] if event_varies else []
                if not fit_names:
                    flat[model] += 1
                    continue
                used[model].update(fit_names)
                train_X = [[value(name, o) for name in fit_names] for o in train]
                train_y = [(log_prices[o + h] - log_prices[o]) - baselines[o]["expected_log_return"] for o in train]
                params, standardised = standardise([list(col) for col in zip(*train_X)])
                X = [[1.0] + [standardised[j][r] for j in range(len(fit_names))] for r in range(len(train))]
                beta = ridge(X, train_y, RIDGE_LAMBDA)
                z = [1.0] + [((value(name, i) - mean) / std if std > 0 else 0.0) for name, (mean, std) in zip(fit_names, params)]
                adjustment = sum(bk * zk for bk, zk in zip(beta, z))
                predicted = b["expected_log_return"] + adjustment
                rows[model].append((t, realised, predicted, predicted - half, predicted + half))
        base = _metrics("baseline", rows["baseline"], None, [])
        baseline_errors = {r[0]: r[1] - r[2] for r in rows["baseline"]}
        model_results = [base]
        for model in ("augmented", "combined"):
            order = list(kept) + ([EVENT_FEATURE] if model == "combined" else [])
            features = [name for name in order if name in used[model]]
            note = None
            if model == "augmented" and not kept:
                note = NO_COMPUTE_FEATURE
            elif model == "combined" and (not event_kept or (trained and flat[model] == trained)):
                note = EVENT_NO_VARIATION
            elif flat[model]:
                what = "every compute feature" if model == "augmented" else "the event feature"
                note = f"{what} had no variation in the trailing window at {flat[model]} of {trained} trained origins: no {model} forecast there"
            model_results.append(_metrics(model, rows[model], baseline_errors, features, note))
        n = base.n
        augmented = model_results[1]
        if n < MIN_N:
            verdict = "insufficient-sample"
            notes.append(f"{n} baseline origins at {h}d; {MIN_N} needed")
        elif augmented.n < MIN_N:
            # The horizon's verdict answers "does the compute information add value"; without enough
            # augmented forecasts (no feature with data, or too few training origins) there is no answer:
            verdict = "insufficient-sample"
            notes.append(f"{augmented.n} augmented forecasts at {h}d ({n} baseline origins); {MIN_N} needed")
        else:
            verdict = augmented.verdict
        if not kept:
            notes.append(NO_COMPUTE_FEATURE + ": augmented reports insufficient-sample rather than a verdict")
        for model in model_results[1:]:
            if model.note and model.note != NO_COMPUTE_FEATURE:
                notes.append(f"{model.model}: {model.note}")
        if dropped:
            notes.append("dropped features: " + ", ".join(f"{k} ({v})" for k, v in dropped.items()))
        results.append(HorizonResult(ticker=ticker, horizon_days=h, origins=n, models=model_results, verdict=verdict, notes=notes))
        for model in MODELS:
            panel[(h, model)] = [(r[0], exits[r[0]], r[1], r[2]) for r in rows[model]]

    # The inputs are the closes and the features that entered the fit: a feature series with no value at any
    # origin (the authored compute series lie outside the window of a real closes series) contributed nothing,
    # so its kind does not label the result. A feature dropped for having no variation did reach the harness.
    absent = {name for name, why in dropped.items() if why == "no data for every origin"}
    feature_provs = [s.points[-1].provenance for name, s in ((price_feature, fcpi), (PRESSURE_FEATURE, pressure_scores), (ENERGY_FEATURE, energy))
                     if s and s.points and name not in absent]
    inputs = [p.provenance for p in points[-1:]] + feature_provs
    prov = derived("walk-forward-ridge-v1", inputs, observed_at=points[-1].t if points else None,
                   note="point-in-time walk-forward; every forecast uses only data observed by its origin")
    kinds = sorted({effective_kind(p) for p in inputs})
    return TickerEvaluation(ticker=ticker, horizons=results, features_available=kept, features_dropped=dropped,
                            feature_sources=feature_sources or {}, input_kinds=kinds, provenance=prov), panel


# -- across tickers: rank skill and the simplest trade (research, never a verdict) -----------------

def _ranks(values: list[float]) -> list[float]:
    """Average ranks, ties sharing the mean of the positions they span."""
    order = sorted(range(len(values)), key=lambda k: values[k])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of the average ranks; None when either side is constant or there are fewer than two pairs."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)


def _t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return statistics.fmean(values) / (sd / math.sqrt(len(values))) if sd > 0 else None


def cross_section(panels: dict[str, Panel], horizons: tuple[int, ...] = HORIZONS, models: tuple[str, ...] = MODELS,
                  min_tickers: int = MIN_CROSS_SECTION) -> list[CrossSectionResult]:
    """Mean Spearman IC per (horizon, model) over the origin dates on which at least `min_tickers` tickers were scored.

    `panels` is one `Panel` per ticker. A date with fewer tickers than the
    minimum is not scored at all (a rank correlation over two names is a coin
    flip); the share of dates with five or more says how wide the cross-section
    really was.
    """
    out = []
    for h in horizons:
        for model in models:
            by_date: dict[datetime, list[tuple[str, float, float]]] = {}
            for ticker, panel in panels.items():
                for t, _exit, realised, predicted in panel.get((h, model), []):
                    by_date.setdefault(t, []).append((ticker, predicted, realised))
            ics, wide, contributing = [], 0, set()
            for t in sorted(by_date):
                pairs = by_date[t]
                if len(pairs) < min_tickers:
                    continue
                ic = spearman([p for _, p, _ in pairs], [r for _, _, r in pairs])
                if ic is None:
                    continue
                ics.append(ic)
                wide += len(pairs) >= WIDE_CROSS_SECTION
                contributing.update(ticker for ticker, _, _ in pairs)
            n = len(ics)
            out.append(CrossSectionResult(horizon_days=h, model=model, n_dates=n,
                                          mean_ic=round(statistics.fmean(ics), 4) if n else None,
                                          t_stat=round(_t_stat(ics), 3) if _t_stat(ics) is not None else None,
                                          share_dates_five_plus=round(wide / n, 4) if n else None, tickers=len(contributing)))
    return out


def non_overlapping(rows: list[ForecastRow]) -> list[ForecastRow]:
    """The origins a trader could actually hold back to back: each opens at or after the previous exit."""
    taken: list[ForecastRow] = []
    for row in sorted(rows, key=lambda r: r[0]):
        if not taken or row[0] >= taken[-1][1]:
            taken.append(row)
    return taken


def tradable(panels: dict[str, Panel], horizons: tuple[int, ...] = HORIZONS, models: tuple[str, ...] = MODELS) -> list[TradableResult]:
    """Sharpe of the sign-of-forecast trade per (horizon, model), pooled over tickers (definition in the module docstring)."""
    out = []
    for h in horizons:
        for model in models:
            excess: list[float] = []
            hits = 0
            contributing = 0
            for ticker, panel in panels.items():
                trades = [row for row in non_overlapping(panel.get((h, model), [])) if row[3] != 0]
                if not trades:
                    continue
                contributing += 1
                drift = statistics.fmean(row[2] for row in trades)
                for _t, _exit, realised, predicted in trades:
                    side = 1.0 if predicted > 0 else -1.0
                    signed = side * realised
                    hits += signed > 0
                    excess.append(signed - side * drift)
            n = len(excess)
            sharpe = mean = None
            if n:
                mean = statistics.fmean(excess)
            if n >= 2:
                # A spread at floating-point noise (every trade the same excess) is no spread: no Sharpe to claim.
                sd = statistics.stdev(excess)
                sharpe = mean / sd * math.sqrt(TRADING_DAYS_PER_YEAR / h) if sd > SPREAD_EPSILON else None
            out.append(TradableResult(horizon_days=h, model=model, n_trades=n, sharpe=round(sharpe, 3) if sharpe is not None else None,
                                      mean_excess_log_return=round(mean, 6) if mean is not None else None,
                                      hit_rate=round(hits / n, 4) if n else None, tickers=contributing))
    return out


def history_coverage(result: TickerEvaluation, closes: domain.Series) -> HistoryCoverage:
    """First and last close, trading days and origins scored per horizon, read off the closes the evaluation ran on."""
    points = closes.points
    first, last = (points[0].t, points[-1].t) if points else (None, None)
    by_horizon = {h.horizon_days: h.origins for h in result.horizons}
    shortest = min(by_horizon) if by_horizon else None
    return HistoryCoverage(ticker=result.ticker, first_close=first, last_close=last, trading_days=len(points),
                           span_years=round((last - first).days / 365.25, 2) if points else None,
                           origins_scored=by_horizon.get(shortest, 0) if shortest is not None else 0,
                           origins_by_horizon=by_horizon, input_kinds=list(result.input_kinds))


# -- the pressure score as a daily series, point in time -------------------------------------

def pressure_score_series(region_id: str, gpu_model_id: str, dates: list[datetime], snapshots: list[tuple[datetime, list[domain.GpuOffer]]], **cell_kwargs) -> domain.Series:
    """The cell's cpx-v1 score at each date, using the offer snapshot known at that date (none before the first).

    Before the first snapshot the premium and availability factors have no
    data and the score comes from the dated factors alone; the series' own
    provenance says so through the cell's coverage.
    """
    from product.flopx import pressure

    ordered = sorted(snapshots, key=lambda s: s[0])
    points = []
    for at in dates:
        known = [rows for t, rows in ordered if t <= at]
        offers = known[-1] if known else []
        cell = pressure.compute_cell(region_id, gpu_model_id, offers=offers, as_of=at, **cell_kwargs)
        points.append(domain.PriceObservation(series_id=f"pressure:{region_id}:{gpu_model_id}", t=at, value=cell.score, unit="score", provenance=cell.provenance))
    return domain.Series(id=f"pressure:{region_id}:{gpu_model_id}", unit="score", points=points)
