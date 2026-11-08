"""Procurement planner: rank the offers that can run a workload, with cost ranges.

For every offer matching the workload (acceptable GPU models, allowed regions,
interruption tolerance vs spot, enough GPUs listed) the plan computes the
GPU-hours, the expected cost, a low/high range, the expected wall-clock
duration (interruptible capacity carries a restart overhead) and a performance
score (FP16 TFLOPS per dollar, memory and interconnect bonuses for the stated
needs). Ranking is expected cost adjusted by performance; trade-offs are
machine tokens the UI maps to copy. Everything is derived from the offers'
provenance tier and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from product.flopx import domain
from product.flopx.adjust import adjust_offer

# Interruptible capacity: expected restart overhead on wall-clock and the
# spread of outcomes, authored assumptions reported as caveats:
SPOT_RESTART_OVERHEAD = {"high": 0.15, "low": 0.35, "none": None}
SPOT_COST_SPREAD = 0.30
ON_DEMAND_COST_SPREAD = 0.05
RESERVED_COST_SPREAD = 0.02


@dataclass(frozen=True)
class PlanOption:
    rank: int
    offer_id: str
    provider_id: str
    region_id: str
    gpu_model_id: str
    contract: str
    gpu_hours: float
    unit_price_usd: float
    expected_cost_usd: float
    cost_low_usd: float
    cost_high_usd: float
    expected_duration_hours: float
    performance_score: float
    trade_offs: list[str] = field(default_factory=list)
    within_budget: bool | None = None
    meets_deadline: bool | None = None


@dataclass(frozen=True)
class Plan:
    plans: list[PlanOption]
    excluded: dict[str, int]
    caveats: list[str]


def _gpu_hours(workload: domain.Workload) -> float:
    if workload.gpu_hours is not None:
        return float(workload.gpu_hours)
    return float(workload.gpu_count or 1) * float(workload.duration_hours)


def _performance(offer: domain.GpuOffer, model: domain.GpuModel, needs: list[str]) -> float:
    score = model.fp16_tflops / max(offer.price_per_gpu_hour_usd, 0.01) / 100.0
    if "high-memory" in needs:
        score *= 1.0 + (model.memory_gb - 80) / 400.0
    if "fast-interconnect" in needs:
        score *= 1.25 if model.interconnect == "NVLink" else 0.75
        if offer.bandwidth_gbps and offer.bandwidth_gbps >= 400:
            score *= 1.1
    if "fp8" in needs:
        score *= 1.2 if model.family in ("Hopper", "Blackwell") else 0.8
    return round(score, 4)


def plan(
    workload: domain.Workload,
    offers: list[domain.GpuOffer],
    gpu_models: dict[str, domain.GpuModel],
    regions: dict[str, domain.Region],
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> Plan:
    now = now or datetime.now(timezone.utc)
    hours = _gpu_hours(workload)
    gpus_needed = workload.gpu_count or max(1, int(round(hours / max(workload.duration_hours, 1e-9))))
    excluded = {"gpu-model": 0, "region": 0, "interruption-tolerance": 0, "gpu-count": 0, "unavailable": 0}
    options: list[PlanOption] = []

    for offer in offers:
        model = gpu_models.get(offer.gpu_model_id)
        if model is None or offer.gpu_model_id not in workload.gpu_model_ids:
            excluded["gpu-model"] += 1
            continue
        if workload.region_ids and offer.region_id not in workload.region_ids:
            excluded["region"] += 1
            continue
        if offer.availability == "unavailable":
            excluded["unavailable"] += 1
            continue
        interruptible = offer.interruptible or offer.contract == "spot"
        overhead = SPOT_RESTART_OVERHEAD[workload.interruption_tolerance]
        if interruptible and overhead is None:
            excluded["interruption-tolerance"] += 1
            continue
        if offer.gpu_count < gpus_needed:
            excluded["gpu-count"] += 1
            continue

        trade_offs: list[str] = []
        unit = offer.price_per_gpu_hour_usd
        expected = unit * hours
        duration = workload.duration_hours
        if interruptible:
            expected *= 1.0 + overhead
            duration *= 1.0 + overhead
            spread = SPOT_COST_SPREAD
            trade_offs.append("interruptible")
        elif offer.contract == "reserved":
            spread = RESERVED_COST_SPREAD
            if offer.min_duration_hours and offer.min_duration_hours > workload.duration_hours:
                expected = unit * gpus_needed * offer.min_duration_hours
                trade_offs.append("minimum-commitment-exceeds-duration")
            trade_offs.append("commitment")
        else:
            spread = ON_DEMAND_COST_SPREAD
        if offer.availability == "limited":
            trade_offs.append("limited-availability")
        if offer.reliability_score is not None and offer.reliability_score < 0.95:
            trade_offs.append("lower-reliability")
        if "fast-interconnect" in workload.performance_needs and model.interconnect != "NVLink":
            trade_offs.append("pcie-interconnect")
        if "high-memory" in workload.performance_needs and model.memory_gb < 80:
            trade_offs.append("low-memory")
        adjusted = adjust_offer(offer, model, regions.get(offer.region_id)).adjusted
        if adjusted > unit * 1.5:
            trade_offs.append("quoted-far-below-comparable")

        within_budget = (expected <= workload.budget_usd) if workload.budget_usd is not None else None
        meets_deadline = None
        if workload.deadline is not None:
            remaining_hours = (workload.deadline - now).total_seconds() / 3600.0
            meets_deadline = duration <= remaining_hours
            if not meets_deadline:
                trade_offs.append("misses-deadline")

        options.append(PlanOption(
            rank=0, offer_id=offer.id, provider_id=offer.provider_id, region_id=offer.region_id,
            gpu_model_id=offer.gpu_model_id, contract=offer.contract, gpu_hours=round(hours, 2),
            unit_price_usd=unit, expected_cost_usd=round(expected, 2),
            cost_low_usd=round(expected * (1 - spread / 2), 2), cost_high_usd=round(expected * (1 + spread), 2),
            expected_duration_hours=round(duration, 2),
            performance_score=_performance(offer, model, workload.performance_needs),
            trade_offs=trade_offs, within_budget=within_budget, meets_deadline=meets_deadline,
        ))

    # Rank: cost per unit of performance, deadline misses last:
    options.sort(key=lambda o: (o.meets_deadline is False, o.expected_cost_usd / max(o.performance_score, 1e-6)))
    ranked = [PlanOption(**{**o.__dict__, "rank": i}) for i, o in enumerate(options[:limit], start=1)]
    caveats = ["advertised-asks-not-quotes", "spot-overhead-assumed", "no-egress-or-storage-costs"]
    if not ranked:
        caveats.append("no-matching-offer")
    return Plan(plans=ranked, excluded=excluded, caveats=caveats)
