"""Series access: sample series from the files, archived observations from the DB.

A series id resolves to exactly one tier. Sample ids come from series.json;
anything else is looked up in `flopx_observations`, where live adapters append.
The two are never merged into one list: that would mix sample with observed
rows, which the provenance rules forbid.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from product.exceptions import UnknownSeriesError
from product.flopx import domain
from product.flopx.data.loader import load_sample
from product.flopx.provenance import utcnow


def _window(points: list[domain.PriceObservation], days: int | None) -> list[domain.PriceObservation]:
    if not days or not points:
        return points
    cutoff = points[-1].t - timedelta(days=days)
    return [p for p in points if p.t >= cutoff]


def archived_series(db: Session, series_id: str, days: int | None = None, *, public_only: bool = False) -> domain.Series | None:
    """Observations a live adapter stored earlier, through the archive's own read path (R1-5): labelled
    `archived`, never `live`, and never a row the licence table or the clock now refuses; None when none are.
    `public_only` is the anonymous reader's view: a `research-archive` source's points stay out."""
    from product.flopx import archive

    return archive.archived_series(db, series_id, since=utcnow() - timedelta(days=days) if days else None, public_only=public_only)


def resolve_series(db: Session | None, series_id: str, days: int | None = None, *, public_only: bool = False) -> domain.Series:
    """Sample first (file-backed), else archived (DB); raises UnknownSeriesError."""
    sample = load_sample().series.get(series_id)
    if sample is not None:
        return domain.Series(id=sample.id, unit=sample.unit, points=_window(sample.points, days))
    if db is not None:
        archived = archived_series(db, series_id, days, public_only=public_only)
        if archived is not None:
            return archived
    raise UnknownSeriesError(series_id=series_id)


def record_observations(db: Session, observations: Iterable[domain.PriceObservation]) -> int:
    """Appends observations idempotently (series_id + t is unique); returns rows added. Goes through the
    archive's write path, so a row the read path would refuse (future-dated, a source that may not be
    stored) is never written here either."""
    from product.flopx import archive

    return archive.record(db, observations, unchanged_interval=timedelta(0))


def values(series: domain.Series) -> list[float]:
    return [p.value for p in series.points]


def last_point(series: domain.Series) -> domain.PriceObservation | None:
    return series.points[-1] if series.points else None


def change_pct(series: domain.Series, days: int) -> float | None:
    """Percent change between the last point and the last point at least `days` earlier."""
    if len(series.points) < 2:
        return None
    last = series.points[-1]
    target = last.t - timedelta(days=days)
    earlier = None
    for p in series.points:
        if p.t <= target:
            earlier = p
        else:
            break
    if earlier is None or earlier.value == 0:
        return None
    return (last.value / earlier.value - 1.0) * 100.0


def point_in_time(series: domain.Series, at: datetime) -> list[domain.PriceObservation]:
    """Only the points known at `at` (inclusive): the no-look-ahead primitive."""
    return [p for p in series.points if p.t <= at]
