"""Load 2026 daily closes from Alpaca Market Data into the research archive, keep them fresh, or remove them.

The founder's CRSP extract (`wrds_load.py`) stops on 2025-12-31. This loader continues each of
the eight `equity:<T>:close` series from 2026-01-01 with the split-adjusted daily bars Alpaca
serves to the founder's own account (`sources/alpaca_data.py`, source `alpaca-data`, licence row
`research-archive`: stored, used by every engine, raw points to signed-in accounts only). The
same module is the CLI (run from `fastapi_backend/`):

    .venv/bin/python -m product.flopx.tools.alpaca_load load [--start 2026-01-01] [--end 2026-11-08] [--dry-run]
    .venv/bin/python -m product.flopx.tools.alpaca_load delete [--dry-run]
    .venv/bin/python -m product.flopx.tools.alpaca_load status

What a bar becomes: the split-adjusted close under `equity:<TICKER>:close`, observed at the
session close in UTC by the rule the Yahoo adapter and the CRSP loader share (21:00 UTC in
winter, 20:00 UTC in summer), provenance `archived`, source `alpaca-data`, carrying the licence
line and, beside it, the bar's open, high, low and volume with the feed and adjustment as an
audit trail the reads ignore. Alpaca's `adjustment=split` and CRSP's `close / cfacpr` are the
same convention, so the two halves of a series join without a jump (pinned over a synthetic
CRSP tail in the tests).

Loading is an idempotent upsert keyed on (series_id, t): a second load of the same window writes
nothing; a bar whose close changed is rewritten. Every load is bounded at one request per ticker
(eight), each 5 s at most, and a ticker whose request fails is reported in `LoadReport.unavailable`
with the reason token while the others still land; nothing raises past `load_window`.

Two callers besides the CLI. `load_at_startup` runs from `product/startup` after the WRDS loaders
(before the refresher starts), asking each ticker only for the days after its newest stored bar
(or from 2026-01-01 when it has none), so a repeat boot moves little data. `refresh_if_due` is the
refresher's once-a-day hook: it runs the same incremental load with its own session at most once
per UTC day across every worker (a marker in the platform cache, set only after a load with no
failed ticker, so a rate-limited day is retried after `RETRY_SECONDS`); without the keys it does
nothing and says so once. `delete` removes every stored row whose source is `alpaca-data` and
nothing else. Known blind spot: the bar for a session still trading is not asked for (the window
ends today, and Alpaca only returns a 1Day bar once the session has closed), so the newest close
appears the first time the hook runs after the close.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from infrastructure.cache import cache
from product.flopx import archive, domain
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import alpaca_data, licences
from product.models import Observation

logger = logging.getLogger(__name__)

SOURCE = alpaca_data.SOURCE
TICKERS = ("APLD", "CORZ", "CRWV", "IREN", "NBIS", "NVDA", "SMCI", "VRT")
FIRST_DAY = date(2026, 1, 1)   # the day after the CRSP extract ends
SERIES_PREFIX = "equity:"
NOTE = "Alpaca Market Data daily close (IEX feed, adjustment=split): continues the CRSP series past 2025-12-31; loaded from the founder's own account"
MARKER_KEY = "flopx:alpaca-data:refreshed"
MARKER_TTL_SECONDS = 24 * 3600
RETRY_SECONDS = 600


@dataclass
class LoadReport:
    start: date
    end: date
    rows: int
    series: int
    inserted: int
    updated: int
    unchanged: int
    first: date | None
    last: date | None
    dry_run: bool
    tickers: dict[str, int] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    requests: int = 0

    def line(self) -> str:
        verb = "would load" if self.dry_run else "loaded"
        text = (f"{SOURCE}: {verb} {self.rows} bars over {self.series} series ({self.first} to {self.last}) for {self.start} to {self.end} "
                f"in {self.requests} requests: {self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged")
        if self.unavailable:
            text += "; unavailable: " + ", ".join(f"{t} ({why})" for t, why in self.unavailable.items())
        return text


def series_id(ticker: str) -> str:
    return f"{SERIES_PREFIX}{ticker.upper()}:close"


def observations(bars: list[domain.EquityBar], *, loaded_at: datetime | None = None) -> tuple[list[domain.PriceObservation], dict[int, dict]]:
    """The archive rows for a set of bars plus, keyed by `id(observation)`, the raw bar fields stored beside each provenance."""
    loaded_at = loaded_at or utcnow()
    licence = licences.licence(SOURCE)
    out: list[domain.PriceObservation] = []
    extra: dict[int, dict] = {}
    for bar in bars:
        prov = Provenance(kind="archived", source=SOURCE, observed_at=bar.t, fetched_at=loaded_at, source_url=alpaca_data.SOURCE_URL,
                          licence=licence, note=NOTE)
        obs = domain.PriceObservation(series_id=series_id(bar.ticker), t=bar.t, value=float(bar.close), unit="USD", provenance=prov)
        out.append(obs)
        extra[id(obs)] = {"alpaca": {"open": bar.open, "high": bar.high, "low": bar.low, "volume": bar.volume,
                                     "feed": alpaca_data.FEED, "adjustment": alpaca_data.ADJUSTMENT}}
    return out, extra


def _rows_by_ticker(db: Session) -> dict[str, list[Observation]]:
    out: dict[str, list[Observation]] = {}
    for row in db.query(Observation).filter(Observation.series_id.like(f"{SERIES_PREFIX}%")).all():
        if str((row.provenance or {}).get("source", "")) == SOURCE:
            out.setdefault(row.series_id.split(":")[1], []).append(row)
    return out


def count_by_ticker(db: Session) -> dict[str, int]:
    """Rows the archive holds per ticker from this source (what `status` and the startup line report)."""
    return dict(sorted((t, len(rows)) for t, rows in _rows_by_ticker(db).items()))


def newest_stored(db: Session) -> dict[str, date]:
    """The session day of the newest stored bar per ticker (the New York day its close belongs to)."""
    out: dict[str, date] = {}
    for ticker, rows in _rows_by_ticker(db).items():
        newest = max(rows, key=lambda r: r.t).t
        newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        out[ticker] = alpaca_data.session_day(newest)
    return out


def load_window(db: Session, starts: dict[str, date], end: date | None = None, *, dry_run: bool = False) -> LoadReport:
    """One request per ticker in `starts` (its own first day) through `end` (today when None); upserts what came back.
    A failed ticker is listed in `unavailable`, never raised; a dry run fetches and counts but writes nothing."""
    today = datetime.now(timezone.utc).date()
    end = end or today
    bars: list[domain.EquityBar] = []
    unavailable: dict[str, str] = {}
    requests = 0
    for ticker in sorted(starts):
        start = starts[ticker]
        if start > end:
            continue   # already holds the window: no request
        requests += 1
        result = alpaca_data.fetch_bars(ticker, start, end)
        if result.ok:
            bars.extend(result.records)
        elif result.reason != "empty-response":
            unavailable[ticker] = result.reason or "unavailable"
    obs, extra = observations(bars)
    per_ticker: dict[str, int] = {}
    for bar in bars:
        per_ticker[bar.ticker] = per_ticker.get(bar.ticker, 0) + 1
    days = [alpaca_data.session_day(b.t) for b in bars]
    report = LoadReport(start=min(starts.values(), default=end), end=end, rows=len(bars), series=len({o.series_id for o in obs}),
                        inserted=0, updated=0, unchanged=0, first=min(days, default=None), last=max(days, default=None), dry_run=dry_run,
                        tickers=dict(sorted(per_ticker.items())), unavailable=unavailable, requests=requests)
    if dry_run:
        held = count_by_ticker(db)
        report.unchanged = sum(held.get(t, 0) for t in starts)
        report.inserted = max(0, len(obs) - report.unchanged)
        return report
    if obs:
        report.inserted, report.updated, report.unchanged = archive.upsert(db, obs, extra=extra)
    return report


def load_range(db: Session, start: date = FIRST_DAY, end: date | None = None, *, tickers: tuple[str, ...] | None = None, dry_run: bool = False) -> LoadReport:
    """The whole window for every ticker (the CLI's load); `tickers` defaults to the eight at call time."""
    return load_window(db, {t: start for t in (tickers or TICKERS)}, end, dry_run=dry_run)


def load_incremental(db: Session, *, tickers: tuple[str, ...] | None = None, today: date | None = None) -> LoadReport:
    """Each ticker from the day after its newest stored bar (FIRST_DAY when it has none) through today."""
    today = today or datetime.now(timezone.utc).date()
    newest = newest_stored(db)
    starts = {t: (newest[t] + timedelta(days=1)) if t in newest else FIRST_DAY for t in (tickers or TICKERS)}
    return load_window(db, starts, today)


def load_at_startup(db: Session) -> LoadReport | None:
    """The startup seam: an incremental load when the keys are present, one log line either way, never raises."""
    if not alpaca_data.configured():
        logger.info("%s: PRODUCT_ALPACA_API_KEY / PRODUCT_ALPACA_SECRET_KEY not set; 2026 closes are not loaded", SOURCE)
        return None
    try:
        report = load_incremental(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("%s: the startup load failed; the research archive stays as it is", SOURCE, exc_info=True)
        return None
    logger.info("%s; per ticker: %s; held: %s", report.line(), report.tickers, count_by_ticker(db))
    if not report.unavailable:
        _mark_refreshed(report.end)
    return report


# -- the refresher's daily hook ---------------------------------------------------------------

_retry_lock = threading.Lock()
_next_attempt = 0.0   # monotonic: no attempt before this (after a failed load)
_said_unconfigured = False


def _marker(day: date) -> str:
    return f"{MARKER_KEY}:{day.isoformat()}"


def _refreshed_today(day: date) -> bool:
    """Whether a load with no failed ticker already ran for `day` in some worker (the platform cache; fail-open to False).
    A miss stores None for a second, the only read the cache API offers; `_mark_refreshed` deletes it first."""
    try:
        return cache.get_or_set(_marker(day), lambda: None, ttl_seconds=1, tags=None) is True
    except Exception:
        return False


def _mark_refreshed(day: date) -> None:
    try:
        cache.delete(_marker(day))
        cache.get_or_set(_marker(day), lambda: True, ttl_seconds=MARKER_TTL_SECONDS, tags=None)
    except Exception:
        logger.warning("%s: the daily marker could not be stored; the next tick may load again (idempotent)", SOURCE)


def refresh_if_due(session_factory, *, now: datetime | None = None) -> LoadReport | None:
    """Called by the refresher every tick; runs the incremental load at most once per UTC day across workers, and
    not within RETRY_SECONDS of a load that had a failed ticker. Returns the report when it ran, None otherwise."""
    global _next_attempt, _said_unconfigured
    if session_factory is None:
        return None
    if not alpaca_data.configured():
        if not _said_unconfigured:
            logger.info("%s: no keys; the daily refresh is off", SOURCE)
            _said_unconfigured = True
        return None
    now = now or utcnow()
    today = now.date()
    with _retry_lock:
        if time.monotonic() < _next_attempt or _refreshed_today(today):
            return None
        _next_attempt = time.monotonic() + RETRY_SECONDS   # one worker's attempt at a time; a success resets nothing, the marker takes over
    db = session_factory()
    try:
        report = load_incremental(db, today=today)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("%s: the daily refresh failed; retried in %ss", SOURCE, RETRY_SECONDS, exc_info=True)
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass
    if report.unavailable:
        logger.warning("%s: %s; retried in %ss", SOURCE, report.line(), RETRY_SECONDS)
    else:
        with _retry_lock:
            _next_attempt = 0.0   # a clean day: the marker alone decides when the next load runs
        _mark_refreshed(today)
        if report.inserted or report.updated:
            logger.info("%s", report.line())
    return report


def reset_refresh_state() -> None:
    """Forgets the in-process retry clock (tests)."""
    global _next_attempt, _said_unconfigured
    with _retry_lock:
        _next_attempt = 0.0
        _said_unconfigured = False


def delete_all(db: Session, *, dry_run: bool = False) -> int:
    """Removes every archived row from this source; returns the count (what would go, when dry)."""
    if dry_run:
        return sum(count_by_ticker(db).values())
    return archive.delete_source(db, SOURCE, series_prefix=SERIES_PREFIX)


# -- CLI ----------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alpaca_load", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="fetch daily bars for the eight tickers and upsert them into the archive")
    load.add_argument("--start", type=date.fromisoformat, default=FIRST_DAY, help=f"first session day (default {FIRST_DAY})")
    load.add_argument("--end", type=date.fromisoformat, default=None, help="last session day (default today)")
    load.add_argument("--dry-run", action="store_true", help="fetch and count; write nothing")
    delete = sub.add_parser("delete", help=f"remove every archived row from source {SOURCE}")
    delete.add_argument("--dry-run", action="store_true", help="count what would go; delete nothing")
    sub.add_parser("status", help="rows held per ticker, with the newest session day")
    args = parser.parse_args(argv)

    from infrastructure.database import SessionLocal

    db = SessionLocal()
    try:
        if args.command == "load":
            if not alpaca_data.configured():
                print("error: PRODUCT_ALPACA_API_KEY and PRODUCT_ALPACA_SECRET_KEY must be set", file=sys.stderr)
                return 1
            report = load_range(db, args.start, args.end, dry_run=args.dry_run)
            print(report.line())
            for ticker, n in report.tickers.items():
                print(f"  {ticker}: {n} bars")
            return 1 if report.unavailable and not report.rows else 0
        if args.command == "delete":
            gone = delete_all(db, dry_run=args.dry_run)
            print(f"{SOURCE}: {'would delete' if args.dry_run else 'deleted'} {gone} rows")
            return 0
        held, newest = count_by_ticker(db), newest_stored(db)
        print(f"{SOURCE}: {sum(held.values())} rows held" + (":" if held else ""))
        for ticker, n in held.items():
            print(f"  {ticker}: {n} (newest {newest[ticker]})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
