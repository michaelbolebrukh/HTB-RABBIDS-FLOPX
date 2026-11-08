"""The FLOPX Compute Price Index (FCPI).

Methodology, in one paragraph, repeated in every payload: for one GPU model, take
every offer in the snapshot, convert each quoted ask to a comparable price with
`adjust.adjust_offer` (interruptible and reserved discounts reversed, regional
cost normalised to the US baseline, memory variant normalised to the model's
reference), and take the MEDIAN. The median is robust to a single outlier ask,
which advertised marketplaces produce routinely. Coverage (providers, regions,
offer count) and the limitations travel with the value. History is the model's
on-demand sample series rescaled so that its last point equals today's adjusted
median, which is stated as a limitation rather than hidden.

The index never mixes tiers: a sample offer set yields a sample index, an
observed offer set yields a derived-from-observed index, and a mixed set is
refused (MixedProvenanceError) rather than averaged.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from product.exceptions import UnknownEntityError
from product.flopx import domain
from product.flopx.adjust import adjust_offer
from product.flopx.data.loader import SampleData
from product.flopx.provenance import assert_unmixed, derived

METHOD = "fcpi-v1:median-of-adjusted-asks"

METHODOLOGY = (
    "Median of comparable asks per GPU model. Each quoted ask is adjusted to an "
    "on-demand, US-baseline, reference-memory price (interruptible and reserved "
    "discounts reversed, regional cost factor removed) before the median is taken. "
    "Coverage counts the providers, regions and offers behind the number."
)

NORMALISATION = [
    "interruptible-discount-reversed",
    "reserved-discount-reversed",
    "region-cost-normalised-to-us",
    "memory-variant-normalised",
]

BASE_LIMITATIONS = [
    "advertised asks, not transacted prices and not utilisation",
    "median of a small population; coverage is reported and must be read with the value",
    "adjustment factors are stated assumptions, not estimated parameters",
]


def compute_index(
    offers: list[domain.GpuOffer],
    gpu_model: domain.GpuModel,
    regions: dict[str, domain.Region],
    *,
    history: domain.Series | None = None,
    as_of: datetime | None = None,
) -> domain.FcpiIndex:
    """One model's index from one offer snapshot (plus optional history to rescale)."""
    rows = [o for o in offers if o.gpu_model_id == gpu_model.id]
    kind = assert_unmixed(o.provenance for o in rows)

    adjusted = [adjust_offer(o, gpu_model, regions.get(o.region_id)).adjusted for o in rows]
    current = statistics.median(adjusted) if adjusted else None

    limitations = list(BASE_LIMITATIONS)
    if len(rows) < 5:
        limitations.append("fewer than five offers behind the median")
    if kind == "sample":
        limitations.append("built on sample offers: illustrative, never observed")

    prov = derived(METHOD, [o.provenance for o in rows], observed_at=as_of)
    values: list[domain.PriceObservation] = []
    if history is not None and history.points and current is not None:
        last = history.points[-1].value
        scale = current / last if last else 1.0
        limitations.append("history is the model's on-demand series rescaled to today's adjusted median")
        values = [
            domain.PriceObservation(
                series_id=f"index:fcpi:{gpu_model.id}", t=p.t, value=round(p.value * scale, 4), unit=p.unit,
                provenance=derived(METHOD, [p.provenance] + [o.provenance for o in rows[:1]], observed_at=p.t),
            )
            for p in history.points
        ]

    return domain.FcpiIndex(
        id=f"index:fcpi:{gpu_model.id}",
        gpu_model_id=gpu_model.id,
        methodology=METHODOLOGY,
        coverage=domain.FcpiCoverage(
            providers=len({o.provider_id for o in rows}),
            regions=len({o.region_id for o in rows}),
            offer_count=len(rows),
        ),
        normalisation=list(NORMALISATION),
        last_updated=max((o.provenance.observed_at for o in rows), default=as_of or prov.observed_at),
        values=values,
        limitations=limitations,
        provenance=prov,
        current=round(current, 4) if current is not None else None,
    )


def sample_index(data: SampleData, gpu_model_id: str) -> domain.FcpiIndex:
    model = data.gpu_models.get(gpu_model_id)
    if model is None:
        raise UnknownEntityError(entity="gpuModel", id=gpu_model_id)
    history = data.series.get(f"gpu:{gpu_model_id}:on-demand:global")
    return compute_index(data.offers, model, data.regions, history=history)


def all_sample_indices(data: SampleData) -> list[domain.FcpiIndex]:
    return [sample_index(data, gid) for gid in data.gpu_models]
