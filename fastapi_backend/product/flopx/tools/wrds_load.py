"""Load the founder's WRDS CRSP daily closes into the archive, or remove them (decision G13).

The input is the CSV `wrds_extract.py` writes (columns `date, ticker, permno, close,
ret, volume, shares_out, cfacpr`), extracted on the founder's machine under the
institutional WRDS licence. It is never committed: Render mounts it as the secret
file `/etc/secrets/flopx-crsp.csv`, and the startup task in `product/startup`
loads it when present. The same module is the CLI (run from `fastapi_backend/`):

    .venv/bin/python -m product.flopx.tools.wrds_load load <csv> [--dry-run]
    .venv/bin/python -m product.flopx.tools.wrds_load delete [--dry-run]
    .venv/bin/python -m product.flopx.tools.wrds_load status

The founder's second extract, the Compustat quarterly fundamentals, has its own loader of
the same shape beside this one: `product/flopx/tools/compustat_load.py` (secret file
`/etc/secrets/flopx-compustat.csv`, source `wrds-compustat`, series `fundamental:<T>:*`).

What a row becomes: CRSP's `close` is the unadjusted price, so the stored value is
`close / cfacpr`, the split-adjusted close (NVDA on 2018-01-02: 199.35 / 40 =
4.98375), under the series id `equity:<TICKER>:close` the rest of FLOPX already
uses. The observation time is the session close in UTC by the convention the Yahoo
adapter uses (`yahoo.bar_close_times`: the 09:30 New York open plus the regular
session), never the load time: 21:00 UTC in winter, 20:00 UTC in summer. Each
row's provenance is `archived`, source `wrds-crsp`, carrying the licence line from
`sources/licences.json` and, beside it, the raw CRSP fields (`permno`, the
unadjusted `close`, `cfacpr`) as an audit trail the reads ignore.

Loading is an idempotent upsert keyed on (series_id, t): a second load of the same
file inserts and rewrites nothing; a row whose value changed is rewritten.
`delete` removes every stored row whose source is `wrds-crsp` and nothing else.
Exit codes: 0 on success, 1 when the file is missing or malformed. Known blind
spot: a ticker the file names that FLOPX does not know is loaded all the same;
`status` lists what the archive holds per ticker so the surplus is visible.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from product.flopx import archive, domain
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import licences, yahoo
from product.models import Observation

logger = logging.getLogger(__name__)

SOURCE = "wrds-crsp"
SECRET_FILE = Path("/etc/secrets/flopx-crsp.csv")
SOURCE_URL = "https://wrds-www.wharton.upenn.edu/"
NOTE = "CRSP daily close divided by the cumulative price adjustment factor (cfacpr): split-adjusted; extracted by the founder under the institutional WRDS licence"
REQUIRED_COLUMNS = ("date", "ticker", "permno", "close", "cfacpr")
EXCHANGE_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)
SERIES_PREFIX = "equity:"


class WrdsLoadError(ValueError):
    """The file is missing, has the wrong columns, or holds a row that cannot be a close."""


@dataclass(frozen=True)
class CrspRow:
    day: date
    ticker: str
    permno: int
    close: float
    cfacpr: float


@dataclass
class LoadReport:
    path: str
    rows: int
    series: int
    inserted: int
    updated: int
    unchanged: int
    first: date | None
    last: date | None
    dry_run: bool
    tickers: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        verb = "would load" if self.dry_run else "loaded"
        return (f"{SOURCE}: {verb} {self.rows} rows over {self.series} series ({self.first} to {self.last}) from {self.path}: "
                f"{self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged")


def series_id(ticker: str) -> str:
    return f"{SERIES_PREFIX}{ticker.upper()}:close"


def adjusted_close(close: float, cfacpr: float) -> float:
    """The split-adjusted close CRSP's cumulative factor implies; a factor of 1 leaves the price alone."""
    if cfacpr <= 0:
        raise WrdsLoadError(f"cfacpr must be positive, got {cfacpr}")
    return close / cfacpr


def session_close_utc(day: date) -> datetime:
    """When that day's close was known: the Yahoo adapter's rule over the New York session open."""
    open_at = datetime(day.year, day.month, day.day, *SESSION_OPEN, tzinfo=EXCHANGE_TZ)
    close_ts = yahoo.bar_close_times([int(open_at.timestamp())], "1d", {})[0]
    return datetime.fromtimestamp(close_ts, tz=timezone.utc)


def read_rows(path: Path) -> list[CrspRow]:
    """Parses the extract; a row with no close (CRSP leaves a delisted day blank) is skipped, a bad header raises."""
    if not path.is_file():
        raise WrdsLoadError(f"no file at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise WrdsLoadError(f"{path} lacks columns {missing}; expected {list(REQUIRED_COLUMNS)}")
        rows: list[CrspRow] = []
        for n, raw in enumerate(reader, start=2):
            close, factor, ticker = (raw.get("close") or "").strip(), (raw.get("cfacpr") or "").strip(), (raw.get("ticker") or "").strip().upper()
            if not close or not factor or not ticker:
                continue
            try:
                rows.append(CrspRow(day=date.fromisoformat(raw["date"].strip()[:10]), ticker=ticker, permno=int(float(raw["permno"])),
                                    close=abs(float(close)), cfacpr=float(factor)))
            except (ValueError, KeyError) as exc:
                raise WrdsLoadError(f"{path} line {n}: {exc}") from exc
    return rows


def observations(rows: list[CrspRow], *, loaded_at: datetime | None = None) -> tuple[list[domain.PriceObservation], dict[int, dict]]:
    """The archive rows for the file plus, keyed by `id(observation)`, the raw CRSP fields stored beside each provenance."""
    loaded_at = loaded_at or utcnow()
    licence = licences.licence(SOURCE)
    out: list[domain.PriceObservation] = []
    extra: dict[int, dict] = {}
    for row in rows:
        t = session_close_utc(row.day)
        prov = Provenance(kind="archived", source=SOURCE, observed_at=t, fetched_at=loaded_at, source_url=SOURCE_URL, licence=licence, note=NOTE)
        obs = domain.PriceObservation(series_id=series_id(row.ticker), t=t, value=adjusted_close(row.close, row.cfacpr), unit="USD", provenance=prov)
        out.append(obs)
        extra[id(obs)] = {"crsp": {"permno": row.permno, "close": row.close, "cfacpr": row.cfacpr}}
    return out, extra


def count_by_ticker(db: Session) -> dict[str, int]:
    """Rows the archive holds per ticker from this source (the status the CLI and the startup line report)."""
    counts: dict[str, int] = {}
    for row in db.query(Observation).filter(Observation.series_id.like(f"{SERIES_PREFIX}%")).all():
        if str((row.provenance or {}).get("source", "")) == SOURCE:
            ticker = row.series_id.split(":")[1]
            counts[ticker] = counts.get(ticker, 0) + 1
    return dict(sorted(counts.items()))


def load_file(db: Session, path: Path, *, dry_run: bool = False) -> LoadReport:
    """Upserts the file into the archive (or, dry, only says what would change); raises WrdsLoadError on a bad file."""
    rows = read_rows(path)
    obs, extra = observations(rows)
    tickers: dict[str, int] = {}
    for row in rows:
        tickers[row.ticker] = tickers.get(row.ticker, 0) + 1
    report = LoadReport(path=str(path), rows=len(rows), series=len({o.series_id for o in obs}), inserted=0, updated=0, unchanged=0,
                        first=min((r.day for r in rows), default=None), last=max((r.day for r in rows), default=None), dry_run=dry_run,
                        tickers=dict(sorted(tickers.items())))
    if dry_run:
        held = count_by_ticker(db)
        report.unchanged = sum(held.values())
        report.inserted = max(0, len(obs) - report.unchanged)
        return report
    report.inserted, report.updated, report.unchanged = archive.upsert(db, obs, extra=extra)
    return report


def delete_all(db: Session, *, dry_run: bool = False) -> int:
    """Removes every archived row from this source; returns the count (what would go, when dry)."""
    if dry_run:
        return sum(count_by_ticker(db).values())
    return archive.delete_source(db, SOURCE, series_prefix=SERIES_PREFIX)


def load_secret_file_if_present(db: Session, path: Path | None = None) -> LoadReport | None:
    """The startup seam: loads the mounted extract when there is one, logs one line either way, never raises.
    The default path is read at call time, so a test can point `SECRET_FILE` elsewhere."""
    path = path or SECRET_FILE
    if not path.is_file():
        logger.info("%s: no extract at %s; the research archive stays as it is", SOURCE, path)
        return None
    try:
        report = load_file(db, path)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("%s: the extract at %s could not be loaded; the research archive stays as it is", SOURCE, path, exc_info=True)
        return None
    logger.info("%s; per ticker: %s", report.line(), report.tickers)
    return report


# -- CLI ----------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wrds_load", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="upsert a CRSP extract into the archive")
    load.add_argument("csv", type=Path)
    load.add_argument("--dry-run", action="store_true", help="parse and count; write nothing")
    delete = sub.add_parser("delete", help=f"remove every archived row from source {SOURCE}")
    delete.add_argument("--dry-run", action="store_true", help="count what would go; delete nothing")
    sub.add_parser("status", help="rows held per ticker")
    args = parser.parse_args(argv)

    from infrastructure.database import SessionLocal

    db = SessionLocal()
    try:
        if args.command == "load":
            try:
                report = load_file(db, args.csv, dry_run=args.dry_run)
            except WrdsLoadError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(report.line())
            for ticker, n in report.tickers.items():
                print(f"  {ticker}: {n} rows in the file")
            return 0
        if args.command == "delete":
            gone = delete_all(db, dry_run=args.dry_run)
            print(f"{SOURCE}: {'would delete' if args.dry_run else 'deleted'} {gone} rows")
            return 0
        held = count_by_ticker(db)
        print(f"{SOURCE}: {sum(held.values())} rows held" + (":" if held else ""))
        for ticker, n in held.items():
            print(f"  {ticker}: {n}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
