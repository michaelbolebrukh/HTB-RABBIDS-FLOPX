"""Wikidata data centres with coordinates (research R3, section F): CC0, sparse.

One SPARQL query for every item that is an instance of a subclass of
`wd:Q671224` (data center) and has a coordinate; 92 to 100 items worldwide, so
the layer is "notable data centres", never a complete map, and every facility
says so. Status is `reported` (Wikidata does not record operating state), the
operator label and inception year travel as source links, and the region is
the country-level mapping from `_mapping.region_for`.
"""

from __future__ import annotations

import logging
import re

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http
from product.flopx.sources._mapping import country_code_for, region_for

logger = logging.getLogger(__name__)

SOURCE = "wikidata"
LICENCE = licences.licence(SOURCE)
ENDPOINT = "https://query.wikidata.org/sparql"
CACHE_TTL_SECONDS = 900
QUERY = (
    "SELECT ?item ?itemLabel ?coord ?countryLabel ?operatorLabel ?inception WHERE { "
    "?item wdt:P31/wdt:P279* wd:Q671224; wdt:P625 ?coord. "
    "OPTIONAL { ?item wdt:P17 ?country. } OPTIONAL { ?item wdt:P137 ?operator. } OPTIONAL { ?item wdt:P571 ?inception. } "
    'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } LIMIT 200'
)
POINT = re.compile(r"Point\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)")


def _value(binding: dict, key: str) -> str | None:
    cell = binding.get(key)
    return str(cell.get("value")) if isinstance(cell, dict) and cell.get("value") is not None else None


def parse(payload: dict, observed_at, fetched_at) -> list[domain.Facility]:
    facilities = []
    seen: set[str] = set()
    for binding in (((payload or {}).get("results") or {}).get("bindings") or []):
        item = _value(binding, "item") or ""
        qid = item.rsplit("/", 1)[-1]
        match = POINT.search(_value(binding, "coord") or "")
        if not qid or qid in seen or not match:
            continue
        seen.add(qid)
        lon, lat = float(match.group(1)), float(match.group(2))
        country = _value(binding, "countryLabel")
        inception = (_value(binding, "inception") or "")[:10]
        facilities.append(domain.Facility(
            id=f"wikidata:{qid}", name=_value(binding, "itemLabel") or qid, lat=lat, lon=lon,
            region_id=region_for(country_code_for(country)), status="reported", operator=_value(binding, "operatorLabel"),
            timeline=domain.FacilityTimeline(announced=inception or None),
            sources=[domain.SourceLink(url=item, date=inception or "", label=f"Wikidata {qid}" + (f" ({country})" if country else ""))],
            provenance=live(SOURCE, observed_at, fetched_at=fetched_at, licence=LICENCE, source_url=item,
                            note=f"notable data centre on Wikidata; operating status not recorded there; {_http.DATE_HEADER_NOTE}"),
        ))
    return facilities


def fetch_facilities() -> SourceResult:
    fetched = _http.fetch_json(SOURCE, ENDPOINT, key="flopx:wikidata:data-centres", ttl_seconds=CACHE_TTL_SECONDS,
                               params={"query": QUERY, "format": "json"}, headers={"Accept": "application/sparql-results+json"})
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    rows = parse(fetched.payload, fetched.observed_at, fetched.fetched_at)
    if not rows:
        return unavailable(SOURCE, "empty-response")
    return available(SOURCE, rows, live(SOURCE, fetched.observed_at, fetched_at=fetched.fetched_at, licence=LICENCE, source_url="https://query.wikidata.org/",
                                        note=f"{len(rows)} geocoded data centres; notable only, not a complete map; {_http.DATE_HEADER_NOTE}"))
