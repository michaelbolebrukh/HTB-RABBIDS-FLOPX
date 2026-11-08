"""RunPod list prices (research R3, section A).

`POST https://api.runpod.io/graphql` with the public `gpuTypes` query, no
Authorization header. Each GPU type carries a secure-cloud and a community-cloud
USD per GPU-hour list price; stock status comes back null keyless for several
types, in which case the offer is labelled `limited` and the note says the
status was not published. RunPod does not publish a region for a list price,
so the region is `global`. No observation time in the body: the response Date
header is used and the note says so.
"""

from __future__ import annotations

import logging

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http
from product.flopx.sources._mapping import gpu_model_for

logger = logging.getLogger(__name__)

SOURCE = "runpod"
LICENCE = licences.licence(SOURCE)
ENDPOINT = "https://api.runpod.io/graphql"
CACHE_TTL_SECONDS = 300
QUERY = "{ gpuTypes { id displayName memoryInGb securePrice communityPrice secureCloud communityCloud maxGpuCount lowestPrice(input:{gpuCount:1}){minimumBidPrice uninterruptablePrice stockStatus} } }"
STOCK = {"High": "available", "Medium": "available", "Low": "limited"}


def _availability(stock: str | None) -> tuple[str, str]:
    if stock in STOCK:
        return STOCK[stock], f"stock status {stock}"
    return "limited", "stock status not published keyless"


def parse(payload: dict, observed_at, fetched_at) -> list[domain.GpuOffer]:
    offers = []
    for raw in (((payload or {}).get("data") or {}).get("gpuTypes") or []):
        model = gpu_model_for(raw.get("displayName"), _http.number(raw.get("memoryInGb"))) or gpu_model_for(raw.get("id"), _http.number(raw.get("memoryInGb")))
        if model is None:
            continue
        lowest = raw.get("lowestPrice") or {}
        availability, stock_note = _availability(lowest.get("stockStatus"))
        for tier, price_key in (("secure", "securePrice"), ("community", "communityPrice")):
            price = _http.number(raw.get(price_key))
            if not price or price <= 0:
                continue
            offers.append(domain.GpuOffer(
                id=f"runpod-{str(raw.get('id')).replace(' ', '-').lower()}-{tier}", provider_id=SOURCE, region_id="global",
                gpu_model_id=model, gpu_count=1, price_per_gpu_hour_usd=round(price, 4), availability=availability,
                interruptible=False, contract="on-demand",
                provenance=live(SOURCE, observed_at, fetched_at=fetched_at, licence=LICENCE, source_url="https://www.runpod.io/pricing",
                                note=f"{tier} cloud list price per GPU-hour, region not published; {stock_note}; {_http.DATE_HEADER_NOTE}"),
            ))
    return offers


def fetch_gpu_offers() -> SourceResult:
    fetched = _http.fetch_json(SOURCE, ENDPOINT, key="flopx:runpod:gpu-types", ttl_seconds=CACHE_TTL_SECONDS, method="POST", json={"query": QUERY})
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    offers = parse(fetched.payload, fetched.observed_at, fetched.fetched_at)
    if not offers:
        return unavailable(SOURCE, "no-mappable-offers")
    prov = live(SOURCE, fetched.observed_at, licence=LICENCE, source_url="https://www.runpod.io/pricing",
                note=f"{len(offers)} list prices; {_http.DATE_HEADER_NOTE}")
    return available(SOURCE, offers, prov)
