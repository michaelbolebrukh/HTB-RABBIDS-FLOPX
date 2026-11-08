"""Change detection between two offer snapshots, with evidence.

Compares offers by id: a price move past the threshold, an availability flip,
an offer that appeared, an offer that disappeared. Every change carries the
before/after values and evidence links to the offer and its model's series, so
a feed card can be drilled into rather than taken on trust. Every change also
carries the provenance of the snapshot its values come from (the "after" offer,
or the "before" offer for a removal), so a sample move reads SAMPLE and a live
one reads LIVE: no authored move is presented as detected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from product.flopx import domain
from product.flopx.provenance import Provenance

PRICE_THRESHOLD = 0.02  # 2% moves and larger are reported


@dataclass(frozen=True)
class OfferChange:
    kind: str                     # price | availability | new | removed
    offer_id: str
    gpu_model_id: str
    provider_id: str
    region_id: str
    before: dict | None = None
    after: dict | None = None
    pct_change: float | None = None
    evidence: list[domain.Evidence] = field(default_factory=list)
    provenance: Provenance | None = None


def _snapshot(offer: domain.GpuOffer) -> dict:
    return {
        "pricePerGpuHourUsd": offer.price_per_gpu_hour_usd,
        "availability": offer.availability,
        "contract": offer.contract,
        "observedAt": offer.provenance.observed_at.isoformat(),
    }


def _evidence(offer: domain.GpuOffer) -> list[domain.Evidence]:
    # The offer opens the GPU offers tab with that row highlighted; the series opens in the
    # research series lab, which reads `series=` (the X-Ray ignores `target=`, R1-10).
    series_id = f"gpu:{offer.gpu_model_id}:on-demand:global"
    return [
        domain.Evidence(label=offer.id, href=f"/gpu?tab=offers&offer={offer.id}", kind="offer"),
        domain.Evidence(label=series_id, href=f"/research?tab=lab&series={series_id}", kind="series"),
    ]


def detect_changes(
    before: list[domain.GpuOffer],
    after: list[domain.GpuOffer],
    *,
    price_threshold: float = PRICE_THRESHOLD,
) -> list[OfferChange]:
    prev = {o.id: o for o in before}
    curr = {o.id: o for o in after}
    changes: list[OfferChange] = []

    for offer_id, now in curr.items():
        was = prev.get(offer_id)
        if was is None:
            changes.append(OfferChange("new", now.id, now.gpu_model_id, now.provider_id, now.region_id,
                                       after=_snapshot(now), evidence=_evidence(now), provenance=now.provenance))
            continue
        if was.price_per_gpu_hour_usd > 0:
            pct = now.price_per_gpu_hour_usd / was.price_per_gpu_hour_usd - 1.0
            if abs(pct) >= price_threshold:
                changes.append(OfferChange("price", now.id, now.gpu_model_id, now.provider_id, now.region_id,
                                           before=_snapshot(was), after=_snapshot(now),
                                           pct_change=round(pct * 100.0, 2), evidence=_evidence(now), provenance=now.provenance))
        if was.availability != now.availability:
            changes.append(OfferChange("availability", now.id, now.gpu_model_id, now.provider_id, now.region_id,
                                       before=_snapshot(was), after=_snapshot(now), evidence=_evidence(now), provenance=now.provenance))

    for offer_id, was in prev.items():
        if offer_id not in curr:
            changes.append(OfferChange("removed", was.id, was.gpu_model_id, was.provider_id, was.region_id,
                                       before=_snapshot(was), evidence=_evidence(was), provenance=was.provenance))

    # Largest moves first, then structural changes:
    order = {"price": 0, "availability": 1, "new": 2, "removed": 3}
    changes.sort(key=lambda c: (order[c.kind], -abs(c.pct_change or 0.0), c.offer_id))
    return changes
