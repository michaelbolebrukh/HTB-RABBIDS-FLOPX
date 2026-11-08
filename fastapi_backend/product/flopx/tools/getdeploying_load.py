"""Load GetDeploying's weekly GPU rental price history into the archive, keep it fresh, or remove it.

A year of weekly medians per model (`sources/getdeploying.py`, source `getdeploying`, CC BY 4.0) is the
history reference FLOPX shows beside its own index while FCPI's archive is still young. The same module
is the CLI (run from `fastapi_backend/`):

    .venv/bin/python -m product.flopx.tools.getdeploying_load load [--dry-run]
    .venv/bin/python -m product.flopx.tools.getdeploying_load delete [--dry-run]
    .venv/bin/python -m product.flopx.tools.getdeploying_load status

What a row becomes: three archived points per model and week, `gpu:<model>:getdeploying:weekly-median`,
`:weekly-min` and `:weekly-max`, unit USD/GPU-hour, observed at the week's end (the Monday 00:00 UTC
after the dataset's Monday `date`), provenance `archived`, source `getdeploying`, the licence line, the
dataset page as source_url, and a note carrying the CC BY 4.0 attribution with the licence link, the week,
the GetDeploying family slug the FLOPX id stands for and the provider and offering counts; beside the
provenance, as an audit trail the reads ignore, the slug, week start, billing type and counts. Nothing
is ever written under `index:fcpi:*` or `gpu:<model>:on-demand:*`: a third-party weekly median is not an
FCPI point, and the pressure engine's momentum rule does not read these series (banked for the founder).

A week whose end has not passed is held back, not stored: the dataset says the newest week changes until
its Sunday is captured, so it is reported as `pending` and lands on the first load after it ends. Loading
is an idempotent upsert keyed on (series_id, t) over the batch's own time window: a reload writes nothing;
a week the upstream revised is rewritten. Every load is one request (the whole CSV, about 1.1 MB), 5 s at
most; a failed request is a `LoadReport` with `unavailable` set and no write, never an exception past
`load`.

Three callers besides the CLI. `load_at_startup` runs from `product/startup` inside `load_research_archive`
(before the refresher starts, so its first snapshots already see the series); it refuses under pytest
unless forced, since the source is keyless and a test must never reach the network. `refresh_if_due` is
the refresher's hook: at most one load per ISO week across every worker (a marker in the platform cache,
set only after a load that fetched; a failed week is retried after `RETRY_SECONDS`, not every tick), and
the first tick of a new week is the moment the just-finished week is final upstream. `delete_all` removes
every row from this source and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from infrastructure.cache import cache
from product.flopx import archive, domain
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import getdeploying, licences
from product.models import Observation

logger = logging.getLogger(__name__)

SOURCE = getdeploying.SOURCE
MARKER_KEY = "flopx:getdeploying:refreshed"
MARKER_TTL_SECONDS = 8 * 24 * 3600
RETRY_SECONDS = 600


@dataclass
class LoadReport:
    rows: int                 # dataset weeks taken (one per model and week; three points each)
    points: int
    series: int
    inserted: int
    updated: int
    unchanged: int
    first_week: date | None
    last_week: date | None
    pending: list[date]       # weeks held back because their end has not passed
    dry_run: bool
    requests: int = 0
    unavailable: str | None = None
    models: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        if self.unavailable:
            return f"{SOURCE}: not loaded ({self.unavailable}); the archive stays as it is"
        verb = "would load" if self.dry_run else "loaded"
        text = (f"{SOURCE}: {verb} {self.rows} weeks over {self.series} series ({self.first_week} to {self.last_week}) as {self.points} points "
                f"in {self.requests} request: {self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged")
        if self.pending:
            text += "; pending until the week ends: " + ", ".join(d.isoformat() for d in self.pending)
        return text


def observations(rows: list[getdeploying.WeeklyPrice], *, loaded_at: datetime | None = None) -> tuple[list[domain.PriceObservation], dict[int, dict]]:
    """The three archive points per week plus, keyed by `id(observation)`, the audit fields stored beside each provenance."""
    loaded_at = loaded_at or utcnow()
    licence = licences.licence(SOURCE)
    out: list[domain.PriceObservation] = []
    extra: dict[int, dict] = {}
    for row in rows:
        note = getdeploying.note_for(row)
        for measure, value in (("weekly-median", row.median), ("weekly-min", row.minimum), ("weekly-max", row.maximum)):
            prov = Provenance(kind="archived", source=SOURCE, observed_at=row.t, fetched_at=loaded_at, source_url=getdeploying.DATASET_PAGE,
                              licence=licence, note=note)
            obs = domain.PriceObservation(series_id=getdeploying.series_id(row.gpu_model_id, measure), t=row.t, value=float(value),
                                          unit=getdeploying.UNIT, provenance=prov)
            out.append(obs)
            extra[id(obs)] = {"getdeploying": {"gpu_slug": row.gpu_slug, "week_start": row.week_start.isoformat(),
                                               "billing_type": getdeploying.BILLING_TYPE, "provider_count": row.provider_count,
                                               "offering_count": row.offering_count}}
    return out, extra


def _rows(db: Session) -> list[Observation]:
    return [row for row in db.query(Observation).filter(Observation.series_id.like(f"{getdeploying.SERIES_PREFIX}%{getdeploying.SERIES_MARK}%")).all()
            if str((row.provenance or {}).get("source", "")) == SOURCE]


def count_by_model(db: Session) -> dict[str, int]:
    """Weekly-median rows held per FLOPX model from this source (one per stored week)."""
    out: dict[str, int] = {}
    for row in _rows(db):
        if row.series_id.endswith(":weekly-median"):
            model = row.series_id.split(":")[1]
            out[model] = out.get(model, 0) + 1
    return dict(sorted(out.items()))


def newest_week(db: Session) -> dict[str, date]:
    """Per model, the Monday the newest stored week began."""
    out: dict[str, date] = {}
    for row in _rows(db):
        if not row.series_id.endswith(":weekly-median"):
            continue
        model = row.series_id.split(":")[1]
        t = row.t if row.t.tzinfo else row.t.replace(tzinfo=timezone.utc)
        start = t.date() - timedelta(days=7)
        if model not in out or start > out[model]:
            out[model] = start
    return out


def load(db: Session, *, now: datetime | None = None, dry_run: bool = False) -> LoadReport:
    """One request for the whole dataset; upserts every mapped week that has ended. Never raises."""
    now = now or utcnow()
    result = getdeploying.fetch_weekly()
    if not result.ok:
        return LoadReport(rows=0, points=0, series=0, inserted=0, updated=0, unchanged=0, first_week=None, last_week=None, pending=[],
                          dry_run=dry_run, requests=1, unavailable=result.reason or "unavailable")
    rows: list[getdeploying.WeeklyPrice] = list(result.records)
    ready = [r for r in rows if r.t <= now]
    pending = sorted({r.week_start for r in rows if r.t > now})
    obs, extra = observations(ready, loaded_at=now)
    per_model: dict[str, int] = {}
    for row in ready:
        per_model[row.gpu_model_id] = per_model.get(row.gpu_model_id, 0) + 1
    report = LoadReport(rows=len(ready), points=len(obs), series=len({o.series_id for o in obs}), inserted=0, updated=0, unchanged=0,
                        first_week=min((r.week_start for r in ready), default=None), last_week=max((r.week_start for r in ready), default=None),
                        pending=pending, dry_run=dry_run, requests=1, models=dict(sorted(per_model.items())))
    if dry_run or not obs:
        if dry_run:
            held = sum(3 * n for n in count_by_model(db).values())
            report.unchanged = min(held, len(obs))
            report.inserted = len(obs) - report.unchanged
        return report
    window = (min(o.t for o in obs), max(o.t for o in obs))
    report.inserted, report.updated, report.unchanged = archive.upsert(db, obs, extra=extra, window=window)
    return report


def load_at_startup(db: Session, *, force: bool = False) -> LoadReport | None:
    """The startup seam: one bounded load, one log line, never raises; refuses under pytest unless `force`
    (the source is keyless, so nothing else keeps a test off the network)."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not force:
        return None
    try:
        report = load(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("%s: the startup load failed; the archive stays as it is", SOURCE, exc_info=True)
        return None
    if report.unavailable:
        logger.warning("%s", report.line())
        return report
    logger.info("%s; per model: %s; held: %s", report.line(), report.models, count_by_model(db))
    _mark_refreshed(_week_of(utcnow()))
    return report


# -- the refresher's weekly hook --------------------------------------------------------------

_retry_lock = threading.Lock()
_next_attempt = 0.0   # monotonic: no attempt before this (after a failed load)


def _week_of(now: datetime) -> str:
    year, week, _ = now.date().isocalendar()
    return f"{year}-W{week:02d}"


def _marker(week: str) -> str:
    return f"{MARKER_KEY}:{week}"


def _refreshed_this_week(week: str) -> bool:
    """Whether a load already fetched for ISO week `week` in some worker (the platform cache; fail-open to False).
    A miss stores None for a second, the only read the cache API offers; `_mark_refreshed` deletes it first."""
    try:
        return cache.get_or_set(_marker(week), lambda: None, ttl_seconds=1, tags=None) is True
    except Exception:
        return False


def _mark_refreshed(week: str) -> None:
    try:
        cache.delete(_marker(week))
        cache.get_or_set(_marker(week), lambda: True, ttl_seconds=MARKER_TTL_SECONDS, tags=None)
    except Exception:
        logger.warning("%s: the weekly marker could not be stored; the next tick may load again (idempotent)", SOURCE)


def refresh_if_due(session_factory, *, now: datetime | None = None, force: bool = False) -> LoadReport | None:
    """Called by the refresher every tick; runs the load at most once per ISO week across workers, and not within
    RETRY_SECONDS of a failed load. Returns the report when it ran, None otherwise. Refuses under pytest unless `force`."""
    global _next_attempt
    if session_factory is None or (os.environ.get("PYTEST_CURRENT_TEST") and not force):
        return None
    now = now or utcnow()
    week = _week_of(now)
    with _retry_lock:
        if time.monotonic() < _next_attempt or _refreshed_this_week(week):
            return None
        _next_attempt = time.monotonic() + RETRY_SECONDS   # one worker's attempt at a time; a success hands over to the marker
    db = session_factory()
    try:
        report = load(db, now=now)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("%s: the weekly refresh failed; retried in %ss", SOURCE, RETRY_SECONDS, exc_info=True)
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass
    if report.unavailable:
        logger.warning("%s; retried in %ss", report.line(), RETRY_SECONDS)
    else:
        with _retry_lock:
            _next_attempt = 0.0
        _mark_refreshed(week)
        if report.inserted or report.updated:
            logger.info("%s", report.line())
    return report


def reset_refresh_state() -> None:
    """Forgets the in-process retry clock (tests)."""
    global _next_attempt
    with _retry_lock:
        _next_attempt = 0.0


def delete_all(db: Session, *, dry_run: bool = False) -> int:
    """Removes every archived row from this source; returns the count (what would go, when dry)."""
    if dry_run:
        return len(_rows(db))
    return archive.delete_source(db, SOURCE, series_prefix=getdeploying.SERIES_PREFIX)


# -- CLI ----------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="getdeploying_load", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    load_cmd = sub.add_parser("load", help="fetch the weekly dataset once and upsert every mapped, completed week")
    load_cmd.add_argument("--dry-run", action="store_true", help="fetch and count; write nothing")
    delete = sub.add_parser("delete", help=f"remove every archived row from source {SOURCE}")
    delete.add_argument("--dry-run", action="store_true", help="count what would go; delete nothing")
    sub.add_parser("status", help="weeks held per model, with the newest week")
    args = parser.parse_args(argv)

    from infrastructure.database import SessionLocal

    db = SessionLocal()
    try:
        if args.command == "load":
            report = load(db, dry_run=args.dry_run)
            print(report.line())
            for model, n in report.models.items():
                print(f"  {model}: {n} weeks")
            return 1 if report.unavailable else 0
        if args.command == "delete":
            gone = delete_all(db, dry_run=args.dry_run)
            print(f"{SOURCE}: {'would delete' if args.dry_run else 'deleted'} {gone} rows")
            return 0
        held, newest = count_by_model(db), newest_week(db)
        print(f"{SOURCE}: {sum(held.values())} weeks held" + (":" if held else ""))
        for model, n in held.items():
            print(f"  {model}: {n} (newest week beginning {newest[model]})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
