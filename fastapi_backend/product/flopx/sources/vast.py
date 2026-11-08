"""Vast.ai marketplace asks (research R3, section A).

`GET https://console.vast.ai/api/v0/bundles/?q=<json>`: keyless, a snapshot of
rentable on-demand offers. Each bundle is one host's offer of `num_gpus` GPUs at
`dph_total` USD per hour for the whole bundle, so the per-GPU price is
`dph_total / num_gpus`. The body carries no observation time, so `observedAt`
is the response `Date` header and the provenance note says so. Vast caps a
search at 64 rows and sorts cheapest first, which would fill the page with
consumer cards; the adapter therefore asks once for the data-centre variants
and once for the rest (two fixed requests per TTL, never paginated).

Licence row `display-not-index` (founder decision G9-b): the asks are a current
market reading. They feed the offers table and the regional pressure grid, are
never archived and never enter an FCPI basket; `READING` (the row's attribution,
"Vast.ai asks: a current market reading, not an index or benchmark") leads every
provenance note this adapter writes, so any payload carrying a Vast row says so.
"""

from __future__ import annotations

import json
import logging

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http
from product.flopx.sources._mapping import gpu_model_for, region_for

logger = logging.getLogger(__name__)

SOURCE = "vast-ai"
LICENCE = licences.licence(SOURCE)   # `display-not-index` in licences.json: shown, never archived, never in an index
READING = licences.attribution(SOURCE)
ENDPOINT = "https://console.vast.ai/api/v0/bundles/"
CACHE_TTL_SECONDS = 300
QUERIES = {
    "dc": ["H100 SXM", "H200", "B200", "A100 SXM4"],
    "edge": ["L40S", "RTX 4090"],
}


def _query(names: list[str]) -> str:
    return json.dumps({"gpu_name": {"in": names}, "rentable": {"eq": True}, "type": "on-demand", "limit": 64, "order": [["dph_total", "asc"]]})


def _offer(raw: dict, observed_at, fetched_at) -> domain.GpuOffer | None:
    model = gpu_model_for(raw.get("gpu_name"), (raw.get("gpu_ram") or 0) / 1024.0)
    count = int(raw.get("num_gpus") or 0)
    total = _http.number(raw.get("dph_total"))
    if model is None or count <= 0 or total is None or total <= 0:
        return None
    geo = str(raw.get("geolocation") or "")
    parts = [p.strip() for p in geo.split(",")]
    country = parts[-1] if parts and len(parts[-1]) == 2 else None
    state = parts[0] if len(parts) > 1 else None
    reliability = _http.number(raw.get("reliability2"))
    down = _http.number(raw.get("inet_down"))
    return domain.GpuOffer(
        id=f"vast-{raw.get('id')}", provider_id=SOURCE, region_id=region_for(country, state), gpu_model_id=model,
        gpu_count=count, price_per_gpu_hour_usd=round(total / count, 4),
        availability="available" if raw.get("rentable") and not raw.get("rented") else "limited",
        interruptible=bool(raw.get("is_bid")), contract="spot" if raw.get("is_bid") else "on-demand",
        reliability_score=round(reliability, 4) if reliability is not None else None,
        bandwidth_gbps=round(down / 1000.0, 3) if down else None, min_duration_hours=None,
        provenance=live(SOURCE, observed_at, fetched_at=fetched_at, licence=LICENCE,
                        source_url=f"https://cloud.vast.ai/?gpu_name={raw.get('gpu_name', '')}",
                        note=f"{READING}. Advertised ask for a {count}-GPU bundle ({geo or 'location unstated'}), not utilisation; {_http.DATE_HEADER_NOTE}"),
    )


def parse(payload: dict, observed_at, fetched_at) -> list[domain.GpuOffer]:
    offers = []
    for raw in (payload or {}).get("offers") or []:
        offer = _offer(raw, observed_at, fetched_at)
        if offer is not None:
            offers.append(offer)
    return offers


def fetch_gpu_offers() -> SourceResult:
    """Rentable on-demand asks mapped onto the six FLOPX models; unavailable when neither request answers."""
    offers: list[domain.GpuOffer] = []
    reasons: list[str] = []
    observed = None
    for name, gpu_names in QUERIES.items():
        fetched = _http.fetch_json(SOURCE, ENDPOINT, key=f"flopx:vast:bundles:{name}", ttl_seconds=CACHE_TTL_SECONDS, params={"q": _query(gpu_names)})
        if isinstance(fetched, _http.Failure):
            reasons.append(fetched.reason)
            continue
        observed = observed or fetched.observed_at
        offers.extend(parse(fetched.payload, fetched.observed_at, fetched.fetched_at))
    if observed is None:
        return unavailable(SOURCE, reasons[0] if reasons else "empty-response")
    if not offers:
        return unavailable(SOURCE, "no-mappable-offers")
    prov = live(SOURCE, observed, licence=LICENCE, source_url="https://cloud.vast.ai/",
                note=f"{READING}. {len(offers)} rentable asks; {_http.DATE_HEADER_NOTE}")
    return available(SOURCE, offers, prov)
