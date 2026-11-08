"""Load the founder's WRDS Compustat quarterly fundamentals into the archive, or remove them (decision G13).

The input is the quarterly CSV `wrds_extract.py` writes (columns `datadate, ticker, gvkey,
revenue_q, capex_ytd, total_assets, report_date, fqtr`), extracted on the founder's machine
under the institutional WRDS licence. It is never committed: Render mounts it as the secret
file `/etc/secrets/flopx-compustat.csv`, and the startup task in `product/startup` loads it
when present, beside the CRSP extract `wrds_load.py` handles. The same module is the CLI
(run from `fastapi_backend/`):

    .venv/bin/python -m product.flopx.tools.compustat_load load <csv> [--dry-run]
    .venv/bin/python -m product.flopx.tools.compustat_load delete [--dry-run]
    .venv/bin/python -m product.flopx.tools.compustat_load status

What a row becomes: up to three archived points per fiscal quarter, under the series ids
`fundamental:<TICKER>:revenue_q`, `fundamental:<TICKER>:capex_q` and
`fundamental:<TICKER>:total_assets`, each dated at the fiscal period end (`datadate`, 00:00
UTC). Compustat reports millions; the stored value is in USD (x 1,000,000), the unit the SEC
EDGAR facts already use, so the two can be compared without a conversion at read time.
`capex_ytd` (Compustat `capxy`) is the fiscal year to date, so the quarter's own spend is
DERIVED: fiscal quarter 1 is the year-to-date figure itself, and a later quarter is its
year-to-date minus the previous quarter's, when the previous row in the file is that
quarter of the same fiscal year with a year-to-date figure of its own. A quarter whose
predecessor is missing or blank gets no capex point (nothing is guessed); the note on every
capex_q point says it is derived. A blank revenue, capex_ytd or total_assets cell yields no
point for that concept.

Each point's provenance is `archived`, source `wrds-compustat`, carrying the licence line
from `sources/licences.json`; `observed_at` is the report date (the day the figure became
public, the SEC adapter's own convention) when the extract has one, else the period end, and
the note names the report date (or its absence) so a point-in-time reader can tell when
the value was knowable. The raw Compustat fields (`gvkey`, `fqtr`, `capex_ytd`, the millions
value, `report_date`) ride beside the provenance under `compustat` as an audit trail the
reads ignore.

Loading is an idempotent upsert keyed on (series_id, t): a second load of the same file
inserts and rewrites nothing; a row whose value changed is rewritten. `delete` removes every
stored row whose source is `wrds-compustat` and nothing else (the SEC-archived
`fundamental:<T>:revenue` rows stay). Exit codes: 0 on success, 1 when the file is missing
or malformed. Known blind spots: a ticker the file names that FLOPX does not know is loaded
all the same (`status` makes the surplus visible); a fiscal year whose quarters are not
consecutive rows in the file derives fewer capex points than it has quarters, which the
load line counts as `capex quarters skipped`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from product.flopx import archive, domain
from product.flopx.provenance import Provenance, utcnow
from product.flopx.sources import licences
from product.models import Observation

logger = logging.getLogger(__name__)

SOURCE = "wrds-compustat"
SECRET_FILE = Path("/etc/secrets/flopx-compustat.csv")
SOURCE_URL = "https://wrds-www.wharton.upenn.edu/"
REQUIRED_COLUMNS = ("datadate", "ticker", "gvkey", "revenue_q", "capex_ytd", "total_assets", "report_date", "fqtr")
SERIES_PREFIX = "fundamental:"
MILLION = 1_000_000.0
UNIT = "USD"
# concept suffix -> (Compustat item, what the point is)
CONCEPTS = {
    "revenue_q": ("revtq", "quarterly revenue"),
    "capex_q": ("capxy", "quarterly capital expenditure, derived from the fiscal year-to-date figure"),
    "total_assets": ("atq", "total assets at the period end"),
}


class CompustatLoadError(ValueError):
    """The file is missing, has the wrong columns, or holds a row that cannot be a fundamental."""


@dataclass(frozen=True)
class CompRow:
    period_end: date
    ticker: str
    gvkey: str
    fqtr: int | None
    revenue_q: float | None       # millions, as Compustat reports
    capex_ytd: float | None       # millions, fiscal year to date
    total_assets: float | None    # millions
    report_date: date | None


@dataclass
class LoadReport:
    path: str
    rows: int
    points: int
    series: int
    inserted: int
    updated: int
    unchanged: int
    capex_skipped: int
    first: date | None
    last: date | None
    dry_run: bool
    tickers: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        verb = "would load" if self.dry_run else "loaded"
        return (f"{SOURCE}: {verb} {self.rows} quarters as {self.points} points over {self.series} series ({self.first} to {self.last}) "
                f"from {self.path}: {self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged; "
                f"{self.capex_skipped} capex quarters skipped (no derivable prior quarter)")


def series_id(ticker: str, concept: str) -> str:
    if concept not in CONCEPTS:
        raise CompustatLoadError(f"unknown concept {concept!r}; one of {sorted(CONCEPTS)}")
    return f"{SERIES_PREFIX}{ticker.upper()}:{concept}"


def period_end_utc(day: date) -> datetime:
    """The fiscal period end as the point's time: midnight UTC of `datadate` (a balance-sheet date, not a session)."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def quarter_start(period_end: date) -> date:
    """The day after the previous fiscal quarter's month end, three months back (2026-07-31 -> 2026-05-01)."""
    month = period_end.month - 3
    year = period_end.year
    if month <= 0:
        month += 12
        year -= 1
    # The last day of that month, plus one day, is the first day of the following month:
    next_month, next_year = (month % 12) + 1, year + (1 if month == 12 else 0)
    return date(next_year, next_month, 1)


def _number(text: str | None) -> float | None:
    text = (text or "").strip()
    return float(text) if text else None


def _day(text: str | None) -> date | None:
    text = (text or "").strip()
    return date.fromisoformat(text[:10]) if text else None


def read_rows(path: Path) -> list[CompRow]:
    """Parses the extract, oldest first per ticker; a bad header or an unparseable cell raises."""
    if not path.is_file():
        raise CompustatLoadError(f"no file at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CompustatLoadError(f"{path} lacks columns {missing}; expected {list(REQUIRED_COLUMNS)}")
        rows: list[CompRow] = []
        for n, raw in enumerate(reader, start=2):
            ticker = (raw.get("ticker") or "").strip().upper()
            if not ticker or not (raw.get("datadate") or "").strip():
                continue
            try:
                fqtr_text = (raw.get("fqtr") or "").strip()
                rows.append(CompRow(
                    period_end=_day(raw["datadate"]), ticker=ticker, gvkey=(raw.get("gvkey") or "").strip(),
                    fqtr=int(float(fqtr_text)) if fqtr_text else None,
                    revenue_q=_number(raw.get("revenue_q")), capex_ytd=_number(raw.get("capex_ytd")),
                    total_assets=_number(raw.get("total_assets")), report_date=_day(raw.get("report_date")),
                ))
            except (ValueError, KeyError) as exc:
                raise CompustatLoadError(f"{path} line {n}: {exc}") from exc
    rows.sort(key=lambda r: (r.ticker, r.period_end))
    return rows


def quarterly_capex(rows: list[CompRow]) -> dict[tuple[str, date], float]:
    """The quarter's own capex in millions, keyed by (ticker, period end), for every row where it can be derived.

    Fiscal quarter 1: the year-to-date figure itself. Quarter n > 1: this row's year-to-date minus the
    previous row's, provided that row is quarter n - 1 of the same ticker (the fiscal year has not rolled
    over) and carries a year-to-date figure. Anything else is left out rather than guessed.
    """
    out: dict[tuple[str, date], float] = {}
    by_ticker: dict[str, list[CompRow]] = {}
    for r in sorted(rows, key=lambda r: (r.ticker, r.period_end)):
        by_ticker.setdefault(r.ticker, []).append(r)
    for ticker, series in by_ticker.items():
        for i, row in enumerate(series):
            if row.capex_ytd is None or row.fqtr is None:
                continue
            if row.fqtr == 1:
                out[(ticker, row.period_end)] = row.capex_ytd
                continue
            prior = series[i - 1] if i > 0 else None
            if prior is None or prior.fqtr != row.fqtr - 1 or prior.capex_ytd is None:
                continue
            out[(ticker, row.period_end)] = row.capex_ytd - prior.capex_ytd
    return out


def _note(concept: str, row: CompRow, *, derived_from: str | None = None) -> str:
    item, what = CONCEPTS[concept]
    period = f"period {quarter_start(row.period_end).isoformat()} to {row.period_end.isoformat()}" if concept != "total_assets" else f"period instant to {row.period_end.isoformat()}"
    reported = f"reported {row.report_date.isoformat()}" if row.report_date else "report date not in the extract (availability unknown; the period end is used)"
    fiscal = f"fiscal quarter {row.fqtr}" if row.fqtr is not None else "fiscal quarter unknown"
    derivation = f"; {derived_from}" if derived_from else ""
    return (f"Compustat {item}: {what}, {fiscal}, {period}, {reported}; millions in the extract, stored in USD{derivation}; "
            "extracted by the founder under the institutional WRDS licence")


def observations(rows: list[CompRow], *, loaded_at: datetime | None = None) -> tuple[list[domain.PriceObservation], dict[int, dict], int]:
    """The archive points for the file, the raw fields stored beside each provenance (keyed by `id(observation)`),
    and how many quarters with a year-to-date capex figure yielded no capex point."""
    loaded_at = loaded_at or utcnow()
    licence = licences.licence(SOURCE)
    capex = quarterly_capex(rows)
    out: list[domain.PriceObservation] = []
    extra: dict[int, dict] = {}
    skipped = 0
    for row in rows:
        t = period_end_utc(row.period_end)
        observed = period_end_utc(row.report_date) if row.report_date else t
        audit = {"gvkey": row.gvkey, "fqtr": row.fqtr, "report_date": row.report_date.isoformat() if row.report_date else None, "capex_ytd": row.capex_ytd}
        values: list[tuple[str, float, str | None]] = []
        if row.revenue_q is not None:
            values.append(("revenue_q", row.revenue_q, None))
        if row.capex_ytd is not None:
            key = (row.ticker, row.period_end)
            if key in capex:
                how = ("the fiscal year-to-date figure itself (quarter 1)" if row.fqtr == 1
                       else f"derived as this quarter's year-to-date capex minus the previous quarter's ({row.capex_ytd:g} less the prior quarter's)")
                values.append(("capex_q", capex[key], how))
            else:
                skipped += 1
        if row.total_assets is not None:
            values.append(("total_assets", row.total_assets, None))
        for concept, millions, how in values:
            prov = Provenance(kind="archived", source=SOURCE, observed_at=observed, fetched_at=loaded_at, source_url=SOURCE_URL, licence=licence,
                              note=_note(concept, row, derived_from=how))
            obs = domain.PriceObservation(series_id=series_id(row.ticker, concept), t=t, value=millions * MILLION, unit=UNIT, provenance=prov)
            out.append(obs)
            extra[id(obs)] = {"compustat": {**audit, "millions": millions}}
    return out, extra, skipped


def count_by_ticker(db: Session) -> dict[str, int]:
    """Rows the archive holds per ticker from this source (the status the CLI and the startup line report)."""
    counts: dict[str, int] = {}
    for row in db.query(Observation).filter(Observation.series_id.like(f"{SERIES_PREFIX}%")).all():
        if str((row.provenance or {}).get("source", "")) == SOURCE:
            ticker = row.series_id.split(":")[1]
            counts[ticker] = counts.get(ticker, 0) + 1
    return dict(sorted(counts.items()))


def load_file(db: Session, path: Path, *, dry_run: bool = False) -> LoadReport:
    """Upserts the file into the archive (or, dry, only says what would change); raises CompustatLoadError on a bad file."""
    rows = read_rows(path)
    obs, extra, skipped = observations(rows)
    tickers: dict[str, int] = {}
    for o in obs:
        ticker = o.series_id.split(":")[1]
        tickers[ticker] = tickers.get(ticker, 0) + 1
    report = LoadReport(path=str(path), rows=len(rows), points=len(obs), series=len({o.series_id for o in obs}), inserted=0, updated=0, unchanged=0,
                        capex_skipped=skipped, first=min((r.period_end for r in rows), default=None), last=max((r.period_end for r in rows), default=None),
                        dry_run=dry_run, tickers=dict(sorted(tickers.items())))
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
    parser = argparse.ArgumentParser(prog="compustat_load", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="upsert a Compustat quarterly extract into the archive")
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
            except CompustatLoadError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(report.line())
            for ticker, n in report.tickers.items():
                print(f"  {ticker}: {n} points in the file")
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
