"""Akash decentralised GPU market (research R3, section A).

`GET https://console-api.akash.network/v1/gpu-prices`: per GPU model, the
number of GPUs on the network and available, and the min/max/avg/median USD per
GPU-hour across providers. One FLOPX offer per matching model row, priced at
the median with the spread in the note; `gpuCount` is the available count. A
small market, labelled as such; the region is `global` (providers are not
located in this endpoint). No observation time in the body: Date header.
"""

from __future__ import annotations

import logging

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http

logger = logging.getLogger(__name__)

SOURCE = "akash"
LICENCE = licences.licence(SOURCE)
ENDPOINT = "https://console-api.akash.network/v1/gpu-prices"
CACHE_TTL_SECONDS = 300
MODELS = {("h100", 80): "h100-sxm-80gb", ("h200", 141): "h200-sxm-141gb", ("b200", 180): "b200-sxm-192gb", ("b200", 192): "b200-sxm-192gb",
          ("a100", 80): "a100-sxm-80gb", ("l40s", 48): "l40s-48gb", ("rtx4090", 24): "rtx-4090-24gb"}


def _ram_gb(text: str | None) -> int | None:
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(digits) if digits else None


def parse(payload: dict, observed_at, fetched_at) -> list[domain.GpuOffer]:
    offers = []
    for raw in (payload or {}).get("models") or []:
        model = MODELS.get((str(raw.get("model") or "").lower(), _ram_gb(raw.get("ram"))))
        price = raw.get("price") or {}
        med = _http.number(price.get("med")) or _http.number(price.get("avg"))
        avail = raw.get("availability") or {}
        total, free = int(avail.get("total") or 0), int(avail.get("available") or 0)
        if model is None or not med or med <= 0 or total <= 0:
            continue
        offers.append(domain.GpuOffer(
            id=f"akash-{raw.get('model')}-{raw.get('ram')}-{raw.get('interface') or 'x'}".lower(), provider_id=SOURCE, region_id="global",
            gpu_model_id=model, gpu_count=max(free, 1), price_per_gpu_hour_usd=round(med, 4),
            availability="available" if free > 0 else "unavailable", interruptible=False, contract="on-demand",
            provenance=live(SOURCE, observed_at, fetched_at=fetched_at, licence=LICENCE, source_url="https://console.akash.network/",
                            note=f"median of {total} listed GPUs ({free} available), min {price.get('min')} max {price.get('max')} USD/GPU-hour; "
                                 f"small decentralised market; {_http.DATE_HEADER_NOTE}"),
        ))
    return offers


def fetch_gpu_offers() -> SourceResult:
    fetched = _http.fetch_json(SOURCE, ENDPOINT, key="flopx:akash:gpu-prices", ttl_seconds=CACHE_TTL_SECONDS)
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    offers = parse(fetched.payload, fetched.observed_at, fetched.fetched_at)
    if not offers:
        return unavailable(SOURCE, "no-mappable-offers")
    prov = live(SOURCE, fetched.observed_at, licence=LICENCE, source_url="https://console.akash.network/",
                note=f"{len(offers)} model medians; {_http.DATE_HEADER_NOTE}")
    return available(SOURCE, offers, prov)
