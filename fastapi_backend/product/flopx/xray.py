"""Market X-Ray: align a target series with overlays, annotate events, correlate.

Alignment is an inner join on calendar date (daily series), each series
normalised to 100 at the first common point for the chart. Correlations are
Pearson on daily log returns, not levels (levels of two trending series
correlate for no reason), at lags from -5 to +5 days (positive lag = overlay
leads the target). Every table row carries n so a reader can see how thin the
evidence is; caveats are tokens.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from product.flopx import domain

MAX_LAG = 5


@dataclass(frozen=True)
class Aligned:
    series_id: str
    unit: str
    points: list[domain.PriceObservation]
    normalised: list[float]


@dataclass(frozen=True)
class Correlation:
    overlay_id: str
    lag_days: int
    pearson: float | None
    n: int


@dataclass(frozen=True)
class EventAnnotation:
    t: datetime
    item_id: str
    kind: str
    headline: str
    severity: int


@dataclass(frozen=True)
class Xray:
    target: Aligned
    overlays: list[Aligned]
    events: list[EventAnnotation]
    correlations: list[Correlation]
    caveats: list[str] = field(default_factory=list)


def _by_date(series: domain.Series) -> dict:
    return {p.t.date(): p for p in series.points}


def align(target: domain.Series, overlays: list[domain.Series]) -> tuple[Aligned, list[Aligned]]:
    maps = [_by_date(target)] + [_by_date(o) for o in overlays]
    common = sorted(set.intersection(*(set(m.keys()) for m in maps))) if maps else []

    def build(series: domain.Series, mapping: dict) -> Aligned:
        points = [mapping[d] for d in common]
        base = points[0].value if points and points[0].value else None
        normalised = [round(p.value / base * 100.0, 4) if base else 0.0 for p in points]
        return Aligned(series_id=series.id, unit=series.unit, points=points, normalised=normalised)

    return build(target, maps[0]), [build(o, m) for o, m in zip(overlays, maps[1:])]


def _log_returns(values: list[float]) -> list[float]:
    out = []
    for a, b in zip(values, values[1:]):
        out.append(math.log(b / a) if a > 0 and b > 0 else 0.0)
    return out


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    try:
        return statistics.correlation(x, y)
    except statistics.StatisticsError:
        return None


def lagged_correlations(target: Aligned, overlay: Aligned, max_lag: int = MAX_LAG) -> list[Correlation]:
    rt = _log_returns([p.value for p in target.points])
    ro = _log_returns([p.value for p in overlay.points])
    rows = []
    for lag in range(-max_lag, max_lag + 1):
        # Positive lag: overlay at t-lag against target at t (overlay leads).
        if lag >= 0:
            x, y = ro[: len(ro) - lag] if lag else ro, rt[lag:]
        else:
            x, y = ro[-lag:], rt[: len(rt) + lag]
        n = min(len(x), len(y))
        rows.append(Correlation(overlay_id=overlay.series_id, lag_days=lag, pearson=_round(pearson(x[:n], y[:n])), n=n))
    return rows


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def annotate(items: list[domain.IntelItem], *, tickers: set[str], gpu_model_ids: set[str], start: datetime, end: datetime) -> list[EventAnnotation]:
    out = []
    for item in items:
        if not (start - timedelta(days=1) <= item.observed_at <= end + timedelta(days=1)):
            continue
        if tickers.intersection(item.entities.tickers) or gpu_model_ids.intersection(item.entities.gpu_model_ids):
            out.append(EventAnnotation(t=item.observed_at, item_id=item.id, kind=item.kind, headline=item.headline, severity=item.severity))
    out.sort(key=lambda e: e.t)
    return out


def _entities(series_ids: list[str]) -> tuple[set[str], set[str]]:
    tickers, gpus = set(), set()
    for sid in series_ids:
        parts = sid.split(":")
        if parts[0] == "equity" and len(parts) > 1:
            tickers.add(parts[1])
        elif parts[0] in ("gpu", "index") and len(parts) > 2:
            gpus.add(parts[2] if parts[0] == "index" else parts[1])
    return tickers, gpus


def xray(target: domain.Series, overlays: list[domain.Series], feed: list[domain.IntelItem]) -> Xray:
    aligned_target, aligned_overlays = align(target, overlays)
    correlations = []
    for overlay in aligned_overlays:
        correlations.extend(lagged_correlations(aligned_target, overlay))
    caveats = ["association-not-causation", "returns-not-levels"]
    kinds = {p.provenance.kind for s in [target, *overlays] for p in s.points}
    if "sample" in kinds:
        caveats.append("sample-data")
    if len(aligned_target.points) < 60:
        caveats.append("short-window")
    if not aligned_target.points:
        caveats.append("no-overlap")
    events: list[EventAnnotation] = []
    if aligned_target.points:
        tickers, gpus = _entities([target.id] + [o.id for o in overlays])
        events = annotate(feed, tickers=tickers, gpu_model_ids=gpus, start=aligned_target.points[0].t, end=aligned_target.points[-1].t)
    return Xray(target=aligned_target, overlays=aligned_overlays, events=events, correlations=correlations, caveats=caveats)
