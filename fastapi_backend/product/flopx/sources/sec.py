"""SEC EDGAR: fundamentals, the filing feed and a one-call peer frame (research R3, section B).

Three public-domain endpoints, all requiring a declared User-Agent with a
contact address and nothing else:

- `companyfacts/CIK##########.json`: every XBRL fact a filer reported. The
  adapter keeps five concepts (revenue, capex, PP&E, net income, remaining
  performance obligations) with tag fallbacks, deduplicated per period, and
  carries each fact's `filed` date as the availability date so nothing is
  "known" before it was public (brief R6, no look-ahead).
- `submissions/CIK##########.json`: the filing feed; 8-K/6-K/10-Q/10-K/20-F
  become verified intelligence items with the acceptance time as publication.
- `frames/us-gaap/<tag>/USD/<period>.json`: one call gives one concept for every
  filer in a calendar period, filtered here to the terminal's six companies.

Limits: 10 requests per second (SEC fair access); the adapter makes one per
call and caches for 15 minutes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http

logger = logging.getLogger(__name__)

SOURCE = "sec-edgar"
LICENCE = licences.licence(SOURCE)
FACTS_ENDPOINT = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_ENDPOINT = "https://data.sec.gov/submissions/CIK{cik}.json"
FRAMES_ENDPOINT = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{period}.json"
CACHE_TTL_SECONDS = 900
USER_AGENT = _http.USER_AGENT

CIKS = {"NVDA": "1045810", "CRWV": "1769628", "NBIS": "1513845", "IREN": "1878848", "APLD": "1144879", "CORZ": "1839341"}
TICKER_BY_CIK = {int(v): k for k, v in CIKS.items()}
# concept -> candidate us-gaap tags, most specific first
CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"],
    "ppe": ["PropertyPlantAndEquipmentNet"],
    "netIncome": ["NetIncomeLoss", "ProfitLoss"],
    "rpo": ["RevenueRemainingPerformanceObligation"],
}
FORMS = {"10-Q", "10-K", "20-F", "6-K", "8-K", "10-K/A", "10-Q/A"}
FEED_FORMS = {"8-K": ("announcement", 2), "6-K": ("announcement", 2), "10-Q": ("earnings", 3), "10-K": ("earnings", 3), "20-F": ("earnings", 3)}
ROWS_PER_CONCEPT = 8


def cik10(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


def _headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _fundamental(ticker: str, concept: str, row: dict, tag: str, fetched_at) -> domain.Fundamental | None:
    value = _http.number(row.get("val"))
    end, filed = row.get("end"), row.get("filed")
    if value is None or not end or not filed:
        return None
    filed_dt = _http.iso(filed)
    return domain.Fundamental(
        ticker=ticker, concept=concept, value=value, unit="USD", period_end=str(end), filed_at=str(filed),
        provenance=live(SOURCE, filed_dt or fetched_at, fetched_at=fetched_at, licence=LICENCE,
                        source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10(CIKS.get(ticker, 0))}&type={row.get('form', '')}",
                        note=f"us-gaap:{tag} from {row.get('form')} ({row.get('fp')} FY{row.get('fy')}), period {row.get('start') or 'instant'} to {end}; filed date is the availability date"),
    )


def parse_facts(payload: dict, ticker: str, fetched_at) -> list[domain.Fundamental]:
    facts = ((payload or {}).get("facts") or {}).get("us-gaap") or {}
    out: list[domain.Fundamental] = []
    for concept, tags in CONCEPTS.items():
        for tag in tags:
            rows = (((facts.get(tag) or {}).get("units") or {}).get("USD")) or []
            rows = [r for r in rows if r.get("form") in FORMS]
            if not rows:
                continue
            # Deduplicate per period end, keeping the first filing of each (the moment it became public):
            by_end: dict[str, dict] = {}
            for r in sorted(rows, key=lambda r: (r.get("end", ""), r.get("filed", ""))):
                by_end.setdefault(r["end"], r)
            kept = sorted(by_end.values(), key=lambda r: r["end"])[-ROWS_PER_CONCEPT:]
            out.extend(f for f in (_fundamental(ticker, concept, r, tag, fetched_at) for r in kept) if f is not None)
            break
    return out


FACT_TAGS = tuple(tag for tags in CONCEPTS.values() for tag in tags)


def slim_facts(payload: dict) -> dict:
    """The companyfacts body cut to the us-gaap tags `parse_facts` reads, same shape (B-MEM). A filer's whole
    body holds every XBRL fact it ever reported: measured 2 to 17 MB of Python objects each for the six terminal
    filers, held for the life of a served process; the kept tags are a few hundred kilobytes."""
    facts = ((payload or {}).get("facts") or {}).get("us-gaap") or {}
    return {"cik": (payload or {}).get("cik"), "entityName": (payload or {}).get("entityName"),
            "facts": {"us-gaap": {tag: facts[tag] for tag in FACT_TAGS if tag in facts}}}


def fetch_fundamentals(cik: str, ticker: str | None = None) -> SourceResult:
    ticker = ticker or TICKER_BY_CIK.get(int(cik), str(cik))
    fetched = _http.fetch_json(SOURCE, FACTS_ENDPOINT.format(cik=cik10(cik)), key=f"flopx:sec:facts:{cik10(cik)}", ttl_seconds=CACHE_TTL_SECONDS,
                               headers=_headers(), reduce=slim_facts)
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    rows = parse_facts(fetched.payload, ticker, fetched.fetched_at)
    if not rows:
        return unavailable(SOURCE, "no-matching-concepts")
    newest = max(f.provenance.observed_at for f in rows)
    return available(SOURCE, rows, live(SOURCE, newest, fetched_at=fetched.fetched_at, licence=LICENCE, note="XBRL company facts; filed date is the availability date"))


def parse_submissions(payload: dict, ticker: str, fetched_at, *, limit: int = 10) -> list[domain.IntelItem]:
    recent = ((payload or {}).get("filings") or {}).get("recent") or {}
    name = str((payload or {}).get("name") or ticker)
    cik = (payload or {}).get("cik")
    forms = recent.get("form") or []
    items: list[domain.IntelItem] = []
    for i, form in enumerate(forms):
        if form not in FEED_FORMS:
            continue
        kind, severity = FEED_FORMS[form]
        accepted = _http.iso((recent.get("acceptanceDateTime") or [None] * len(forms))[i]) or _http.iso((recent.get("filingDate") or [None] * len(forms))[i]) or fetched_at
        accession = str((recent.get("accessionNumber") or [""] * len(forms))[i])
        document = str((recent.get("primaryDocument") or [""] * len(forms))[i])
        description = str((recent.get("primaryDocDescription") or [""] * len(forms))[i]) or form
        href = f"https://www.sec.gov/Archives/edgar/data/{int(cik) if cik else 0}/{accession.replace('-', '')}/{document}" if accession and document else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10(cik or 0)}"
        items.append(domain.IntelItem(
            id=f"sec:{accession}", kind=kind, headline=f"{name} filed {form}: {description}",
            why_it_matters="A primary-source filing; what it says is in the document itself, not in this card.",
            published_at=accepted, observed_at=accepted, severity=severity, classification="verified",
            entities=domain.IntelEntities(tickers=[ticker]),
            evidence=[domain.Evidence(label=f"{form} {accession}", href=href, kind="filing")],
            provenance=live(SOURCE, accepted, fetched_at=fetched_at, licence=LICENCE, source_url=href, note="EDGAR filing index; acceptance time is the publication time"),
        ))
        if len(items) >= limit:
            break
    return items


def fetch_filings(cik: str, ticker: str | None = None, *, limit: int = 10) -> SourceResult:
    ticker = ticker or TICKER_BY_CIK.get(int(cik), str(cik))
    fetched = _http.fetch_json(SOURCE, SUBMISSIONS_ENDPOINT.format(cik=cik10(cik)), key=f"flopx:sec:submissions:{cik10(cik)}", ttl_seconds=CACHE_TTL_SECONDS, headers=_headers())
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    items = parse_submissions(fetched.payload, ticker, fetched.fetched_at, limit=limit)
    if not items:
        return unavailable(SOURCE, "no-recent-filings")
    return available(SOURCE, items, live(SOURCE, items[0].published_at, fetched_at=fetched.fetched_at, licence=LICENCE, note="EDGAR submissions feed"))


def latest_closed_quarter(today: date | None = None, *, settle_days: int = 45) -> str:
    """The most recent calendar quarter that ended at least `settle_days` ago, as a frames period (CY2026Q2)."""
    today = today or datetime.now(timezone.utc).date()
    cursor = date.fromordinal(today.toordinal() - settle_days)
    quarter = (cursor.month - 1) // 3  # 0-based quarter containing cursor; the last CLOSED quarter is the one before it
    year = cursor.year
    if quarter == 0:
        year, quarter = year - 1, 4
    return f"CY{year}Q{quarter}"


def parse_frame(payload: dict, concept: str, fetched_at) -> list[domain.Fundamental]:
    tag = str((payload or {}).get("tag") or "")
    out = []
    for row in (payload or {}).get("data") or []:
        ticker = TICKER_BY_CIK.get(int(row.get("cik") or 0))
        if ticker is None:
            continue
        value = _http.number(row.get("val"))
        if value is None or not row.get("end"):
            continue
        # Frames carry no filed date; the period end is the earliest honest availability bound and the note says so:
        out.append(domain.Fundamental(
            ticker=ticker, concept=concept, value=value, unit="USD", period_end=str(row["end"]), filed_at=str(row["end"]),
            provenance=live(SOURCE, _http.iso(row["end"]) or fetched_at, fetched_at=fetched_at, licence=LICENCE,
                            source_url=f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{payload.get('ccp')}.json",
                            note=f"us-gaap:{tag} for {payload.get('ccp')} from the frames endpoint; filedAt unknown here, set to the period end"),
        ))
    return out


def fetch_frame(concept: str = "capex", period: str | None = None) -> SourceResult:
    """One concept for every filer in one calendar period, kept for the six terminal companies."""
    tag = CONCEPTS[concept][0]
    period = period or latest_closed_quarter()
    fetched = _http.fetch_json(SOURCE, FRAMES_ENDPOINT.format(tag=tag, period=period), key=f"flopx:sec:frame:{tag}:{period}", ttl_seconds=CACHE_TTL_SECONDS, headers=_headers())
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    rows = parse_frame(fetched.payload, concept, fetched.fetched_at)
    if not rows:
        return unavailable(SOURCE, "no-terminal-companies-in-frame")
    return available(SOURCE, rows, live(SOURCE, max(f.provenance.observed_at for f in rows), fetched_at=fetched.fetched_at, licence=LICENCE, note=f"frames {tag} {period}"))
