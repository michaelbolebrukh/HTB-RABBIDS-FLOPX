"""Company fundamentals from two observed sources, merged by one rule, and the public figures derived from them.

Two sources report a company's quarterly fundamentals: live SEC EDGAR facts (`sources/sec.py`, the
values as filed, source `sec-edgar`) and the founder's WRDS Compustat extract in the archive
(`tools/compustat_load.py`, source `wrds-compustat`, licence use `research-archive`). The merge
rule, in one sentence: where both report the same concept for the same period, the live SEC
value stands and the Compustat value is dropped; Compustat supplies every period SEC did not
answer for, which in practice is the history (SEC keeps eight rows per concept) and, when SEC
is down or has not yet got the filing, the latest quarter. Each row keeps its own provenance,
so the payload says which source each figure came from.

"The same period" is the concept plus the period's length class plus the month its period
ends in: a fiscal quarter ending 2026-07-26 in SEC's filing and 2026-07-31 in Compustat
(which normalises `datadate` to the month end) are one quarter; a 10-K's twelve-month revenue
is never replaced by, nor compared with, a Compustat quarter. The length class is read off
the provenance note's `period <start> to <end>` clause, which both the SEC adapter and the
Compustat loader write (the old frontend reads the same clause); a row without one is
`other` and only ever matches rows of its own kind.

`summarise` derives the two public figures the company surfaces show, from rows of the quarter
class only: revenue growth year on year (the latest quarter against the quarter ending about a
year earlier, within 14 days, the frontend's own like-for-like rule) and capex intensity (the
latest quarter's capital expenditure over the same quarter's revenue). Both are `derived`
provenances whose `inputKinds` and `sources` name what they were computed from; a company with
no like-for-like pair gets no figure rather than a comparison of unequal periods. Authored
sample fundamentals carry no period clause, so they never produce a figure: a derived number
rests on observed rows or does not exist. The raw Compustat rows themselves follow the
`research-archive` gate in `terminal.py`; the figures here are public because they are derived.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable

from product.flopx import domain
from product.flopx.provenance import Provenance, derived, effective_kind

METHOD = "fundamentals-v1"
SEC_SOURCE = "sec-edgar"
COMPUSTAT_SOURCE = "wrds-compustat"
SERIES_PREFIX = "fundamental:"
# archived series suffix -> the concept name the company payload uses (SEC's vocabulary where it has one)
CONCEPT_BY_SUFFIX = {"revenue_q": "revenue", "capex_q": "capex", "total_assets": "totalAssets"}
PERIOD_RE = re.compile(r"period (\d{4}-\d{2}-\d{2}|instant) to (\d{4}-\d{2}-\d{2})")
QUARTER_DAYS = (80, 100)
ANNUAL_DAYS = (350, 380)
YEAR_TOLERANCE = timedelta(days=14)
RATIO = "ratio"
GROWTH = "revenueGrowthYoY"
INTENSITY = "capexIntensity"
MERGE_RULE = ("live SEC EDGAR facts stand where both sources report the same period; WRDS Compustat supplies the "
              "periods SEC did not answer for; each row's provenance names its source")


def period_class(f: domain.Fundamental) -> str:
    """`quarter`, `annual`, `instant` (a balance-sheet value) or `other` (no period clause, or an unusual length)."""
    match = PERIOD_RE.search(f.provenance.note or "")
    if match is None:
        return "other"
    if match.group(1) == "instant":
        return "instant"
    days = (date.fromisoformat(match.group(2)) - date.fromisoformat(match.group(1))).days
    if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
        return "quarter"
    if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
        return "annual"
    return "other"


def _end(f: domain.Fundamental) -> date:
    return date.fromisoformat(f.period_end[:10])


def period_key(f: domain.Fundamental) -> tuple[str, str, str]:
    """What makes two rows the same figure: concept, length class and the month the period ends in."""
    return f.concept, period_class(f), f.period_end[:7]


def from_archive(series: Iterable[domain.Series]) -> list[domain.Fundamental]:
    """Archived `fundamental:<T>:<suffix>` series as company fundamentals: one row per point, the concept mapped
    onto the payload's vocabulary, `filedAt` the point's availability date (its provenance `observed_at`, which
    the Compustat loader sets to the report date when the extract has one, else the period end)."""
    out: list[domain.Fundamental] = []
    for s in series:
        parts = s.id.split(":")
        if len(parts) != 3 or parts[0] + ":" != SERIES_PREFIX or parts[2] not in CONCEPT_BY_SUFFIX:
            continue
        ticker, concept = parts[1], CONCEPT_BY_SUFFIX[parts[2]]
        for p in s.points:
            observed = p.provenance.observed_at
            out.append(domain.Fundamental(ticker=ticker, concept=concept, value=p.value, unit=p.unit, period_end=_date_text(p.t),
                                          filed_at=_date_text(observed), provenance=p.provenance))
    out.sort(key=lambda f: (f.concept, f.period_end))
    return out


def _date_text(value: datetime | date) -> str:
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def merge(primary: Iterable[domain.Fundamental], secondary: Iterable[domain.Fundamental]) -> list[domain.Fundamental]:
    """`primary` rows (SEC) plus every `secondary` row (Compustat) whose period key no primary row holds; sorted."""
    kept: dict[tuple[str, str, str], domain.Fundamental] = {}
    for f in primary:
        kept.setdefault(period_key(f), f)
    for f in secondary:
        kept.setdefault(period_key(f), f)
    return sorted(kept.values(), key=lambda f: (f.concept, f.period_end, f.provenance.source))


def _quarters(rows: Iterable[domain.Fundamental], concept: str) -> list[domain.Fundamental]:
    return sorted((f for f in rows if f.concept == concept and period_class(f) == "quarter"), key=_end)


def _year_earlier(latest: domain.Fundamental, rows: list[domain.Fundamental]) -> domain.Fundamental | None:
    target = _end(latest) - timedelta(days=365)
    candidates = [f for f in rows if f is not latest and abs(_end(f) - target) <= YEAR_TOLERANCE]
    return min(candidates, key=lambda f: abs(_end(f) - target)) if candidates else None


def _same_quarter(anchor: domain.Fundamental, rows: list[domain.Fundamental]) -> domain.Fundamental | None:
    candidates = [f for f in rows if abs(_end(f) - _end(anchor)) <= YEAR_TOLERANCE]
    return min(candidates, key=lambda f: abs(_end(f) - _end(anchor))) if candidates else None


def _figure(concept: str, value: float, latest: domain.Fundamental, other: domain.Fundamental, *, compared_to: str | None, note: str) -> dict:
    inputs = [latest.provenance, other.provenance]
    return {
        "concept": concept, "value": value, "unit": RATIO, "period_end": latest.period_end, "compared_to": compared_to,
        "sources": sorted({p.source for p in inputs}),
        "provenance": derived(METHOD, inputs, note=note),
    }


def revenue_growth(rows: Iterable[domain.Fundamental]) -> dict | None:
    """Latest quarterly revenue over the quarter ending about a year earlier, minus one; None without a like-for-like pair."""
    quarters = _quarters(rows, "revenue")
    for latest in reversed(quarters[-3:]):
        prior = _year_earlier(latest, quarters)
        if prior is not None and prior.value:
            return _figure(GROWTH, latest.value / prior.value - 1.0, latest, prior, compared_to=prior.period_end,
                           note=f"quarterly revenue for the period ending {latest.period_end} ({latest.provenance.source}) against the quarter "
                                f"ending {prior.period_end} ({prior.provenance.source}); a ratio, 0.25 is 25% growth")
    return None


def capex_intensity(rows: Iterable[domain.Fundamental]) -> dict | None:
    """The latest quarter's capital expenditure over that quarter's revenue; None unless both quarterly rows exist."""
    capex = _quarters(rows, "capex")
    revenue = _quarters(rows, "revenue")
    for latest in reversed(capex[-3:]):
        rev = _same_quarter(latest, revenue)
        if rev is not None and rev.value:
            return _figure(INTENSITY, latest.value / rev.value, latest, rev, compared_to=None,
                           note=f"quarterly capital expenditure ({latest.provenance.source}) over quarterly revenue ({rev.provenance.source}) "
                                f"for the period ending {latest.period_end}; a ratio of revenue")
    return None


def latest_by_concept(rows: Iterable[domain.Fundamental]) -> list[dict]:
    """The newest row per concept with the source it came from (the quarter class where one exists, else any)."""
    out: list[dict] = []
    by_concept: dict[str, list[domain.Fundamental]] = {}
    for f in rows:
        by_concept.setdefault(f.concept, []).append(f)
    for concept in sorted(by_concept):
        candidates = by_concept[concept]
        preferred = [f for f in candidates if period_class(f) in ("quarter", "instant")] or candidates
        newest = max(preferred, key=lambda f: (f.period_end, f.filed_at))
        out.append({"concept": concept, "value": newest.value, "unit": newest.unit, "period_end": newest.period_end, "filed_at": newest.filed_at,
                    "period_class": period_class(newest), "source": newest.provenance.source, "kind": effective_kind(newest.provenance)})
    return out


def summarise(rows: Iterable[domain.Fundamental]) -> dict:
    """The company payload's `fundamentalsSummary`: the newest value per concept with its source, the derived public
    figures, the distinct sources behind the rows, and the merge rule in words."""
    rows = list(rows)
    figures = [f for f in (revenue_growth(rows), capex_intensity(rows)) if f is not None]
    latest = latest_by_concept(rows)
    sources = sorted({f.provenance.source for f in rows})
    missing = [name for name, present in ((GROWTH, any(f["concept"] == GROWTH for f in figures)), (INTENSITY, any(f["concept"] == INTENSITY for f in figures))) if not present]
    note = MERGE_RULE
    if missing:
        note += f"; no like-for-like quarterly pair for {', '.join(missing)}, so no figure is stated"
    return {"latest": latest, "figures": figures, "sources": sources, "note": note}


def provenances(summary: dict) -> list[Provenance]:
    return [f["provenance"] for f in summary.get("figures", [])]
