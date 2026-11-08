"""Fill the archive with real power-price history from the two openly licensed day-ahead sources (N-DATA).

The live adapters in `sources/energy.py` only ever ask for yesterday and today, so the archive
held a few days of DE-LU and GB prices at most and the pressure engine's power-stress factor
(a 30-day trailing median) had nothing to stand on. Both upstreams publish their history
keylessly under licences that allow storing it, and this module loads it:

- Energy-Charts (Fraunhofer ISE), `energy:DE-LU:day-ahead`, CC BY 4.0 with the attribution in
  every response body (`license_info`), one request covers months (a two-year window measured
  at 810 KB on 2026-11-08: 15-minute slots since October 2025, hourly before);
- Elexon BMRS, `energy:GB:market-index`, the BMRS open data licence (attribution required), at
  most seven days per request (the API answers 400 past that), so two years is about 105 calls.

Each point is the upstream's own delivery slot, parsed by the same `parse_energy_charts` /
`parse_elexon` the live path uses, stored with provenance kind `archived`, the source id, the
licence line from `sources/licences.json` and a note naming the slot, the attribution and the
fact that it was loaded by this backfill; the request window and run time ride beside the
provenance under `backfill` as an audit trail the reads ignore. Writes go through
`archive.upsert`, so a second run writes nothing and a corrected price is rewritten. There is no
per-point correction of the live rows: a slot the live adapter already archived is the same
(series_id, t) key and is left alone when its value matches.

The load is RESUMABLE and BOUNDED. A run first reads which UTC days the archive already holds a
row for in the target window (the last `HISTORY_DAYS` days up to yesterday), fetches only the
gaps, consecutive missing days grouped into one request up to the source's chunk size, and stops
after `max_requests` requests or `budget_seconds`, reporting what is still missing; the next run
picks up the remainder. A day the upstream has no data for is asked for again on every run (one
request per gap, so it costs little); a day the upstream answered partially counts as covered and
is not re-asked, which is the one known blind spot. A point the archive would refuse (a source
whose licence row is no longer archivable, a future-dated slot) is skipped by `upsert` itself.

Two ways to run it. On Render, the startup task `start_history_backfill` (product/startup) starts
one daemon thread per process which waits `START_DELAY_SECONDS` so the refresher's boot warm-up
finishes first, then does the load under the cross-worker advisory lock `flopx-history-backfill`,
so the gunicorn workers take turns and the second finds nothing missing (one query per series).
It refuses to start under pytest. The CLI (run from `fastapi_backend/`):

    .venv/bin/python -m product.flopx.tools.history_backfill run [--days N] [--until YYYY-MM-DD] [--max-requests N] [--dry-run]
    .venv/bin/python -m product.flopx.tools.history_backfill status

`run --dry-run` lists the gaps and the requests it would make and fetches nothing. Exit codes:
0 when every gap is filled or the run stopped at its bound, 1 when a source answered with an
error for every request. Requests bypass the refresher's serving mode on purpose (they go
through `_http._request`, the one-shot primitive with the per-host spacing and `Retry-After`
blocks, never the snapshot cache), because in a served process `fetch_json` answers from the
newest live snapshot and would never reach the history endpoint.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from product.flopx import archive, domain
from product.flopx.provenance import utcnow
from product.flopx.sources import _http, eia, energy, licences
from product.models import Observation

logger = logging.getLogger(__name__)

HISTORY_DAYS = 730
MAX_REQUESTS = 150
BUDGET_SECONDS = 900.0
START_DELAY_SECONDS = 180.0      # after the refresher's boot warm-up and its first recompose have settled
PAUSE_SECONDS = 1.0            # between two requests to the same upstream (politeness, and headroom for the request threads on a 0.5-CPU instance)
LOCK_NAME = "flopx-history-backfill"
UTC = timezone.utc


@dataclass(frozen=True)
class HistorySource:
    source: str
    series_id: str
    chunk_days: int
    url: str
    params: Callable[[date, date], dict]          # inclusive first and last UTC day wanted
    parse: Callable[[object, datetime], list[domain.PriceObservation]]
    source_url: str
    enabled: Callable[[], bool] = lambda: True   # a keyed source is skipped, and says so, while its key is absent


def _energy_charts_params(first: date, last: date) -> dict:
    # Energy-Charts reads the window as local (CET) calendar days, so the slot at 23:00 UTC of `last` is
    # the first slot of the next local day: one extra day is asked for and the UTC-day filter trims it.
    return {"bzn": "DE-LU", "start": first.isoformat(), "end": (last + timedelta(days=1)).isoformat()}


def _elexon_params(first: date, last: date) -> dict:
    # From and To inclusive may span at most seven days (measured 2026-11-08: eight answers 400).
    return {"from": f"{first.isoformat()}T00:00Z", "to": f"{(last + timedelta(days=1)).isoformat()}T00:00Z", "dataProviders": "APXMIDP"}


SOURCES: dict[str, HistorySource] = {
    "energy:DE-LU:day-ahead": HistorySource(
        source=energy.ENERGY_CHARTS, series_id="energy:DE-LU:day-ahead", chunk_days=60, url=energy.ENERGY_CHARTS_ENDPOINT,
        params=_energy_charts_params, parse=lambda payload, fetched_at: energy.parse_energy_charts(payload, "DE-LU", fetched_at),
        source_url="https://api.energy-charts.info/price"),
    "energy:GB:market-index": HistorySource(
        source=energy.ELEXON, series_id="energy:GB:market-index", chunk_days=6, url=energy.ELEXON_ENDPOINT,
        params=_elexon_params, parse=energy.parse_elexon, source_url="https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index"),
    # N-EIA: ERCOT and PJM hourly demand and day-ahead demand forecast (MWh, never a price), one type per request so
    # 200 days x 24 hours = 4800 rows stay under the API's 5000-row cap; two years is four requests per series.
    # Loaded only while EIA_API_KEY is set; the key rides in the params and `_request` never logs a query string.
    **{sid: HistorySource(
        source=eia.SOURCE, series_id=sid, chunk_days=eia.HISTORY_CHUNK_DAYS, url=eia.ENDPOINT,
        params=(lambda first, last, sid=sid: eia.history_params(sid, first, last)),
        parse=(lambda payload, fetched_at, sid=sid: eia.parse_series(sid, payload, fetched_at)),
        source_url=eia.SOURCE_URL, enabled=eia.configured) for sid in eia.SERIES_IDS},
}


@dataclass
class SourceReport:
    series_id: str
    source: str
    window: tuple[date, date]
    days_wanted: int
    days_held_before: int
    gaps: list[tuple[date, date]] = field(default_factory=list)
    requests: int = 0
    failures: list[str] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    days_still_missing: int = 0
    stopped_at_bound: bool = False

    def line(self, *, dry_run: bool = False) -> str:
        verb = "would fetch" if dry_run else "fetched"
        head = (f"{self.source}: {self.series_id} {self.window[0]} to {self.window[1]}: {self.days_held_before}/{self.days_wanted} days held before, "
                f"{len(self.gaps)} gaps, {verb} {self.requests} requests")
        if dry_run:
            return head
        tail = f"; {self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged; {self.days_still_missing} days still missing"
        if self.failures:
            tail += f"; failures: {', '.join(sorted(set(self.failures)))}"
        if self.stopped_at_bound:
            tail += "; stopped at the run's bound, the next run continues"
        return head + tail


@dataclass
class RunReport:
    sources: list[SourceReport]
    dry_run: bool
    seconds: float

    @property
    def complete(self) -> bool:
        return all(r.days_still_missing == 0 for r in self.sources)

    @property
    def all_failed(self) -> bool:
        asked = [r for r in self.sources if r.requests]
        return bool(asked) and all(r.failures and len(r.failures) == r.requests for r in asked)

    def lines(self) -> list[str]:
        return [r.line(dry_run=self.dry_run) for r in self.sources]


class BackfillError(RuntimeError):
    """A CLI argument that cannot describe a window."""


# -- what is held, what is missing -------------------------------------------------------------

def window_for(until: date, days: int = HISTORY_DAYS) -> tuple[date, date]:
    """The inclusive UTC days to fill: `days` days ending the day before `until` (today's slots are the live adapter's)."""
    last = until - timedelta(days=1)
    return last - timedelta(days=days - 1), last


def held_days(db: Session, series_id: str, first: date, last: date) -> set[date]:
    """Every UTC day in [first, last] the archive holds at least one row for under `series_id`."""
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    end = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    rows = db.query(Observation.t).filter(Observation.series_id == series_id, Observation.t >= start, Observation.t < end).distinct().all()
    return {archive._aware(t).date() for (t,) in rows}


def gaps_in(held: set[date], first: date, last: date, chunk_days: int) -> list[tuple[date, date]]:
    """Consecutive missing days grouped into inclusive (first, last) chunks of at most `chunk_days`."""
    out: list[tuple[date, date]] = []
    day, run_start = first, None
    while day <= last + timedelta(days=1):
        missing = day <= last and day not in held
        if missing and run_start is None:
            run_start = day
        if missing and (day - run_start).days + 1 == chunk_days:
            out.append((run_start, day))
            run_start = None
        elif not missing and run_start is not None:
            out.append((run_start, day - timedelta(days=1)))
            run_start = None
        day += timedelta(days=1)
    return out


# -- one chunk ------------------------------------------------------------------------------------

def fetch_chunk(src: HistorySource, first: date, last: date) -> _http.Fetched | _http.Failure:
    """One request for the inclusive UTC days [first, last], outside the snapshot cache; never raises."""
    try:
        return _http._request(src.source, "GET", src.url, params=src.params(first, last), headers=None, json=None, as_json=True)
    except _http._Short as short:
        return short.failure
    except Exception:
        logger.warning("%s: history request raised", src.source, exc_info=True)
        return _http.Failure("adapter-error")


def observations_for(src: HistorySource, payload, fetched_at: datetime, first: date, last: date, *, loaded_at: datetime | None = None,
                     ) -> tuple[list[domain.PriceObservation], dict[int, dict]]:
    """The chunk's points inside the requested UTC days, provenance `archived` with the backfill named, plus the audit extras."""
    loaded_at = loaded_at or utcnow()
    licence = licences.licence(src.source)
    out: list[domain.PriceObservation] = []
    extra: dict[int, dict] = {}
    for p in src.parse(payload, fetched_at):
        day = archive._aware(p.t).date()
        if day < first or day > last:
            continue
        prov = replace(p.provenance, kind="archived", licence=licence, fetched_at=loaded_at,
                       note=f"{p.provenance.note}; loaded by history_backfill from the upstream history endpoint")
        obs = replace(p, provenance=prov)
        out.append(obs)
        extra[id(obs)] = {"backfill": {"window": [first.isoformat(), last.isoformat()], "run_at": loaded_at.isoformat(), "upstream_fetched_at": archive._aware(fetched_at).isoformat()}}
    return out, extra


# -- the run ---------------------------------------------------------------------------------------

def run(db: Session, *, until: date | None = None, days: int = HISTORY_DAYS, max_requests: int = MAX_REQUESTS,
        budget_seconds: float = BUDGET_SECONDS, dry_run: bool = False, series_ids: list[str] | None = None,
        pause_seconds: float = PAUSE_SECONDS) -> RunReport:
    """Fills every gap in the window for each enabled source, within the bounds; see the module docstring."""
    started = time.monotonic()
    until = until or utcnow().date()
    first, last = window_for(until, days)
    reports: list[SourceReport] = []
    requests_left = max_requests
    for sid, src in SOURCES.items():
        if series_ids and sid not in series_ids:
            continue
        if not licences.archivable(src.source):
            logger.info("%s: licence row no longer allows archiving; history not loaded", src.source)
            continue
        if not src.enabled():
            logger.info("%s: %s not configured (no key); history not loaded", src.source, sid)
            continue
        held = held_days(db, sid, first, last)
        gaps = gaps_in(held, first, last, src.chunk_days)
        report = SourceReport(series_id=sid, source=src.source, window=(first, last), days_wanted=(last - first).days + 1,
                              days_held_before=len(held), gaps=gaps)
        missing = {g for a, b in gaps for g in _days(a, b)}
        if dry_run:
            report.requests = len(gaps)
            report.days_still_missing = len(missing)
            reports.append(report)
            continue
        for a, b in gaps:
            if requests_left <= 0 or time.monotonic() - started > budget_seconds:
                report.stopped_at_bound = True
                break
            if report.requests:
                time.sleep(pause_seconds)
            requests_left -= 1
            report.requests += 1
            fetched = fetch_chunk(src, a, b)
            if isinstance(fetched, _http.Failure):
                report.failures.append(fetched.reason)
                logger.warning("%s: history %s to %s not fetched (%s)", src.source, a, b, fetched.reason)
                continue
            points, extra = observations_for(src, fetched.payload, fetched.fetched_at, a, b)
            if not points:
                report.failures.append("empty-response")
                continue
            try:
                ins, upd, same = archive.upsert(db, points, extra=extra, window=(min(p.t for p in points), max(p.t for p in points)))
            except Exception:
                db.rollback()
                report.failures.append("write-failed")
                logger.warning("%s: history %s to %s not written", src.source, a, b, exc_info=True)
                continue
            report.inserted += ins
            report.updated += upd
            report.unchanged += same
            missing -= {archive._aware(p.t).date() for p in points}
        report.days_still_missing = len(missing)
        reports.append(report)
    return RunReport(sources=reports, dry_run=dry_run, seconds=time.monotonic() - started)


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def status(db: Session, *, until: date | None = None, days: int = HISTORY_DAYS) -> list[dict]:
    """Per series: the window, how many of its days the archive holds, and the oldest and newest row held."""
    until = until or utcnow().date()
    first, last = window_for(until, days)
    out = []
    for sid, src in SOURCES.items():
        if not src.enabled():
            continue
        held = held_days(db, sid, first, last)
        out.append({"series_id": sid, "source": src.source, "window": (first, last), "days_wanted": (last - first).days + 1,
                    "days_held": len(held), "oldest": min(held) if held else None, "newest": max(held) if held else None})
    return out


# -- the startup seam ------------------------------------------------------------------------------

_state: dict = {"pid": None, "thread": None}
_state_lock = threading.Lock()


def _work(session_factory: Callable[[], Session], **kwargs) -> None:
    db = session_factory()
    try:
        report = run(db, **kwargs)
    finally:
        db.close()
    for line in report.lines():
        logger.info("history backfill: %s", line)


def _thread_main(session_factory: Callable[[], Session], delay: float, run_kwargs: dict) -> None:
    try:
        from product.flopx.refresher import lower_thread_priority
        lower_thread_priority()
        if delay:
            time.sleep(delay)
        from infrastructure.database import run_with_advisory_lock
        run_with_advisory_lock(LOCK_NAME, lambda: _work(session_factory, **run_kwargs))
    except Exception:
        logger.warning("history backfill did not complete; the next boot continues it", exc_info=True)


def start_background(session_factory: Callable[[], Session] | None = None, *, force: bool = False, delay: float = START_DELAY_SECONDS,
                     **run_kwargs) -> bool:
    """Starts this process's one backfill thread (idempotent per process); refuses under pytest unless `force`.
    Returns whether a thread was started."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not force:
        return False
    with _state_lock:
        thread = _state["thread"]
        if _state["pid"] == os.getpid() and thread is not None and thread.is_alive():
            return False
        if session_factory is None:
            from infrastructure.database import SessionLocal
            session_factory = SessionLocal
        thread = threading.Thread(target=_thread_main, args=(session_factory, delay, run_kwargs), name="flopx-history-backfill", daemon=True)
        _state.update(pid=os.getpid(), thread=thread)
        thread.start()
        logger.info("FLOPX history backfill scheduled in pid %s (after %.0f s)", os.getpid(), delay)
        return True


# -- CLI ---------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history_backfill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    runp = sub.add_parser("run", help="fill the gaps in the archive's power-price history")
    runp.add_argument("--days", type=int, default=HISTORY_DAYS, help=f"days of history to hold, ending yesterday (default {HISTORY_DAYS})")
    runp.add_argument("--until", type=date.fromisoformat, default=None, help="the day after the last day wanted (default today, UTC)")
    runp.add_argument("--max-requests", type=int, default=MAX_REQUESTS)
    runp.add_argument("--series", action="append", default=None, help="only this series id (repeatable)")
    runp.add_argument("--dry-run", action="store_true", help="list the gaps and the requests; fetch nothing")
    statp = sub.add_parser("status", help="days held per series in the window")
    statp.add_argument("--days", type=int, default=HISTORY_DAYS)
    statp.add_argument("--until", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    if args.days < 1:
        print("error: --days must be at least 1", file=sys.stderr)
        return 1

    from infrastructure.database import SessionLocal

    db = SessionLocal()
    try:
        if args.command == "run":
            report = run(db, until=args.until, days=args.days, max_requests=args.max_requests, dry_run=args.dry_run, series_ids=args.series)
            for line in report.lines():
                print(line)
            if report.dry_run:
                for r in report.sources:
                    for a, b in r.gaps:
                        print(f"  {r.series_id}: {a} to {b}")
            return 1 if report.all_failed else 0
        for row in status(db, until=args.until, days=args.days):
            print(f"{row['source']}: {row['series_id']} holds {row['days_held']}/{row['days_wanted']} days of {row['window'][0]} to {row['window'][1]}"
                  + (f" (oldest {row['oldest']}, newest {row['newest']})" if row["oldest"] else ""))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
