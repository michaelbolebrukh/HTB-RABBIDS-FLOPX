"""Quoted price -> comparable price, with the breakdown that makes it fair.

Every step is a named multiplicative factor with the assumption behind it, so
"the cheapest comparable offer" is inspectable. Assumptions are module
constants, documented, and reported back in `reference` so the UI can show
what the adjustment assumed rather than present the number as a fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from product.flopx import domain

# Interruptible (spot) capacity is quoted at a discount to on-demand; to compare
# a spot ask with an on-demand ask the discount is reversed. 55% is the
# authored sample assumption for the discount, stated here and in the output:
INTERRUPTIBLE_DISCOUNT = 0.55
# Reserved commitments discount against on-demand for the commitment, not for
# the compute; reversed the same way:
RESERVED_DISCOUNT = 0.30
# Regional cost multipliers relative to the US baseline (power, land, tax):
REGION_FACTORS = {"US": 1.00, "IE": 1.05, "DE": 1.08, "NO": 0.95, "SG": 1.12, "JP": 1.10}
# Memory variant normalisation: price per GB of HBM is not linear, so a square
# root keeps a 141 GB part from looking 76% "more expensive" than an 80 GB one:
MEMORY_EXPONENT = 0.5


@dataclass(frozen=True)
class AdjustmentStep:
    name: str
    factor: float
    note: str


@dataclass(frozen=True)
class Adjustment:
    quoted: float
    adjusted: float
    steps: list[AdjustmentStep]
    reference: dict = field(default_factory=dict)


def adjust_offer(
    offer: domain.GpuOffer,
    gpu_model: domain.GpuModel,
    region: domain.Region | None = None,
    *,
    reference_memory_gb: int | None = None,
) -> Adjustment:
    """Returns the comparable on-demand, US-baseline, reference-memory price for an offer."""
    steps: list[AdjustmentStep] = []
    price = offer.price_per_gpu_hour_usd

    if offer.contract == "spot" or offer.interruptible:
        factor = 1.0 / (1.0 - INTERRUPTIBLE_DISCOUNT)
        steps.append(AdjustmentStep("interruptible", factor, f"spot ask grossed up by the assumed {INTERRUPTIBLE_DISCOUNT:.0%} interruptible discount"))
    elif offer.contract == "reserved":
        factor = 1.0 / (1.0 - RESERVED_DISCOUNT)
        steps.append(AdjustmentStep("reserved", factor, f"reserved ask grossed up by the assumed {RESERVED_DISCOUNT:.0%} commitment discount"))
    else:
        steps.append(AdjustmentStep("contract", 1.0, "on-demand ask, no contract adjustment"))

    country = (region.country_code if region else "US").upper()
    region_factor = REGION_FACTORS.get(country, 1.0)
    steps.append(AdjustmentStep("region", 1.0 / region_factor, f"normalised from the {country} cost level ({region_factor:.2f}x) to the US baseline"))

    ref_memory = reference_memory_gb or gpu_model.memory_gb
    if ref_memory != gpu_model.memory_gb:
        memory_factor = (ref_memory / gpu_model.memory_gb) ** MEMORY_EXPONENT
        steps.append(AdjustmentStep("memory", memory_factor, f"{gpu_model.memory_gb} GB variant scaled to the {ref_memory} GB reference (exponent {MEMORY_EXPONENT})"))
    else:
        steps.append(AdjustmentStep("memory", 1.0, f"reference memory {ref_memory} GB, no variant adjustment"))

    adjusted = price
    for step in steps:
        adjusted *= step.factor

    return Adjustment(
        quoted=round(price, 4),
        adjusted=round(adjusted, 4),
        steps=steps,
        reference={
            "contract": "on-demand",
            "regionBaseline": "US",
            "memoryGb": ref_memory,
            "interruptibleDiscount": INTERRUPTIBLE_DISCOUNT,
            "reservedDiscount": RESERVED_DISCOUNT,
        },
    )
