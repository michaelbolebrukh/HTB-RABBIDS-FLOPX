"""Exposure tracing (architecture §10.5): which listed companies a regional condition reaches, and on what basis.

`documented` is true only for a link an OBSERVED source substantiates: the
company is named as owner (`facility-owner`) or operator (`facility-operator`)
of a facility whose record was observed (live or archived, e.g. Wikidata), or
a filing sourced from SEC EDGAR mentions the region or one of its facilities
(`filing-mention`). The same bases resting on an authored sample facility, and
every link resting on the authored company profile (`sample-profile`, the
`computeExposure` tags), are `documented: false`: an authored link, which the
UI labels as such (V1 F7: a sample "Ashburn campus" owner is not a document). `sensitivity` is the authored per-company sensitivity (`scenario.company_sensitivity`, by profile role; V4 F5)
stated as an estimate; whether a measured relationship exists
is the evaluation harness's question (§10.6), never this module's claim.

N-XRAY: every row also states its tie in words (`role`, `tie_kind`, `tie_basis`, `tie_note`), documented rows
first, so the X-Ray's Financial exposure tab lists companies for every covered region and labels the authored
ones "authored tie from company profile, not a filing" rather than showing nothing.
"""

from __future__ import annotations

from product.flopx import domain
from product.flopx.provenance import Provenance, derived, effective_kind
from product.flopx.scenario import EQUITY_ELASTICITIES, ROLE_SENSITIVITY, company_sensitivity

SENSITIVITY_KEY = "demandGrowthPct"
SENSITIVITY_BASIS = (
    f"authored elasticity EQUITY_ELASTICITIES[{SENSITIVITY_KEY!r}] in scenario.py (percent equity move per "
    "percent compute-demand move); an estimate, not a measured beta"
)
# Profile tags that imply exposure to a cell, by what they reach:
TAGS_EVERY_CELL = ("accelerator-vendor", "picks-and-shovels", "gpu-cloud", "neocloud", "data-centre-developer", "hosting")
TAGS_BY_REGION_PREFIX = {"europe": "eu-"}

Filing = domain.IntelItem


def _filing_mentions(filing: Filing, region: domain.Region, facilities: list[domain.Facility]) -> bool:
    text = f"{filing.headline} {filing.why_it_matters}".lower()
    if region.id in filing.geography.region_ids:
        return True
    if region.name.split(" (")[0].lower() in text:
        return True
    return any(f.id in filing.entities.facility_ids or f.name.lower() in text for f in facilities)


def _observed(facility: domain.Facility) -> bool:
    return effective_kind(facility.provenance) in ("live", "archived")


AUTHORED_TIE_NOTE = "authored tie from company profile, not a filing"
ROLE_NONE = "no compute role in the profile"


def company_role(company: domain.Company) -> str:
    """The profile role the sensitivity is read off (the same tag `scenario.company_sensitivity` names), or
    `ROLE_NONE`."""
    roles = [t for t in ROLE_SENSITIVITY if t in company.compute_exposure.tags]
    return max(roles, key=lambda t: ROLE_SENSITIVITY[t]) if roles else ROLE_NONE


def _facility_words(facility: domain.Facility) -> str:
    kind = "observed" if _observed(facility) else "authored sample"
    return f"{facility.name} ({facility.id}, {facility.status}, {kind} record)"


def _region_words(region: domain.Region | None, region_id: str) -> str:
    return region.name if region is not None else region_id


def trace(
    region_id: str,
    gpu_model_id: str | None,
    *,
    companies: dict[str, domain.Company],
    facilities: list[domain.Facility],
    regions: dict[str, domain.Region],
    filings: list[Filing] = (),
) -> list[domain.Exposure]:
    """Every company the region reaches, documented ties first (N-XRAY): a row whose tie an observed facility
    record or an SEC filing substantiates is `tie_kind: documented`; every other row is `tie_kind: authored`
    (the company profile's tags, or an authored sample facility) and says so in `tie_note`, so a covered region
    never answers with an empty list and never passes an authored tie off as a document."""
    region = regions.get(region_id)
    region_name = _region_words(region, region_id)
    in_region = [f for f in facilities if f.region_id == region_id]
    rows: list[domain.Exposure] = []
    for ticker in sorted(companies):
        c = companies[ticker]
        basis: list[str] = []
        linked: list[str] = []
        provs: list[Provenance] = [c.provenance]
        documented_words: list[str] = []
        authored_words: list[str] = []
        owned = [f for f in in_region if ticker in f.owner_company_ids]
        if owned:
            basis.append("facility-owner")
            linked += [f.id for f in owned]
            provs += [f.provenance for f in owned]
            for f in owned:
                (documented_words if _observed(f) else authored_words).append(f"owner of record of {_facility_words(f)}")
        operated = [f for f in in_region if f.operator and f.operator.lower() == c.name.lower()]
        if operated:
            basis.append("facility-operator")
            linked += [f.id for f in operated if f.id not in linked]
            provs += [f.provenance for f in operated]
            for f in operated:
                (documented_words if _observed(f) else authored_words).append(f"operator of {_facility_words(f)}")
        if region is not None:
            mentions = [f for f in filings if ticker in f.entities.tickers and f.provenance.source.startswith("sec")
                        and _filing_mentions(f, region, in_region)]
            if mentions:
                basis.append("filing-mention")
                provs += [f.provenance for f in mentions]
                documented_words += [f"SEC filing names the region: {m.headline}" for m in mentions]
        tags = set(c.compute_exposure.tags)
        every_cell = sorted(tags.intersection(TAGS_EVERY_CELL))
        by_prefix = [tag for tag, prefix in TAGS_BY_REGION_PREFIX.items() if tag in tags and region_id.startswith(prefix)]
        if every_cell or by_prefix:
            basis.append("sample-profile")
            if every_cell:
                authored_words.append(f"profile role {', '.join(every_cell)} reaches every region")
            if by_prefix:
                authored_words.append(f"profile tag {', '.join(by_prefix)} reaches {region_name}")
        if not basis:
            continue
        documented = bool(documented_words)
        tie_kind = "documented" if documented else "authored"
        tie_basis = "; ".join(documented_words if documented else authored_words)
        tie_note = None if documented else AUTHORED_TIE_NOTE
        prov = derived("exposure-v1", provs, note=(
            "links from observed facility records and SEC filings" if documented else AUTHORED_TIE_NOTE))
        rows.append(domain.Exposure(
            ticker=ticker, basis=basis, documented=documented, sensitivity=company_sensitivity(c)[0],
            sensitivity_basis=company_sensitivity(c)[1], facility_ids=sorted(set(linked)), provenance=prov,
            role=company_role(c), tie_kind=tie_kind, tie_basis=tie_basis, tie_note=tie_note,
        ))
    rows.sort(key=lambda e: (not e.documented, -len(e.facility_ids), e.ticker))
    return rows


def documented_tickers(exposures: list[domain.Exposure]) -> list[str]:
    return [e.ticker for e in exposures if e.documented]
