"""Effective supply per region (architecture §10.2): an INFERENCE with its assumptions exposed.

Operating megawatts are what the facility records say is operating; under
construction and announced (announced + planned) are listed apart and never
summed into operating; `reported` records are listed and counted nowhere.
`effectiveMw = operatingMw x utilisation + sum(underConstruction_i x completionProbability_i)`
where utilisation is an ASSUMPTION (default 0.70, range 0.5-0.9, never observed)
and `completionProbability_i = clamp(1 - monthsLate_i / 24, 0, 1) x 0.8` from the
facility's `timeline.expectedOnline` (a facility with no date gets 0.25).
GPU-equivalents are given only where every counted facility carries a
`gpuCountEstimate`, otherwise the payload says how many do and gives MW only.

One provenance class per region (`one_class_facilities`): where the authored
sample set has facility records for a region, only those are counted; observed
(Wikidata) records count only for a region the sample does not cover. A sum of
live and sample megawatts would be an aggregate over two classes, so the other
class is named in the note and left out, never added in.
"""

from __future__ import annotations

from datetime import datetime

from product.flopx import domain
from product.flopx.provenance import derived, effective_kind

METHOD = "effective-supply-v1"
DEFAULT_UTILISATION = 0.70
UTILISATION_RANGE = [0.5, 0.9]
COMPLETION_CEILING = 0.8
LATE_HORIZON_MONTHS = 24.0
NO_DATE_PROBABILITY = 0.25
ANNOUNCED_STATUSES = ("announced", "planned")


def one_class_facilities(facilities: list[domain.Facility], region_id: str) -> tuple[list[domain.Facility], int, str]:
    """The region's facility records in one provenance class, how many of the other class were left out, and the class kept.

    Sample records win where the region has any (the authored set is the region's complete
    illustrative picture); observed records (live or archived) are used only where it has none.
    """
    rows = [f for f in facilities if f.region_id == region_id]
    sample = [f for f in rows if effective_kind(f.provenance) == "sample"]
    if sample:
        return sample, len(rows) - len(sample), "sample"
    return rows, 0, "observed" if rows else "none"


def class_note(kept: str, left_out: int) -> str:
    """The sentence that names the class an aggregate was computed over and what it left out."""
    if not left_out:
        return ""
    return (f"; computed over the region's {kept} facility records only, {left_out} observed record"
            f"{'s' if left_out != 1 else ''} for the region listed apart and not mixed in")


def months_late(expected_online: str | None, as_of: datetime) -> float | None:
    """Months by which `expected_online` precedes `as_of` (0 when still in the future); None without a date."""
    if not expected_online:
        return None
    try:
        year, month, day = (int(part) for part in expected_online.split("-")[:3])
    except ValueError:
        return None
    months = (as_of.year - year) * 12 + (as_of.month - month) + (as_of.day - day) / 30.0
    return round(max(0.0, months), 2)


def completion_probability(expected_online: str | None, as_of: datetime) -> tuple[float, float | None]:
    late = months_late(expected_online, as_of)
    if late is None:
        return NO_DATE_PROBABILITY, None
    return round(max(0.0, min(1.0, 1.0 - late / LATE_HORIZON_MONTHS)) * COMPLETION_CEILING, 4), late


def assumptions(utilisation: float) -> list[domain.Assumption]:
    return [
        domain.Assumption("utilisation", utilisation, list(UTILISATION_RANGE),
                          "share of operating capacity assumed in use; an assumption, never observed"),
        domain.Assumption("completion-ceiling", COMPLETION_CEILING, [0.5, 1.0],
                          "an on-time under-construction facility counts at most this share of its stated capacity"),
        domain.Assumption("late-horizon-months", LATE_HORIZON_MONTHS, [12.0, 36.0],
                          "completion probability falls linearly to zero this many months past the expected online date"),
        domain.Assumption("no-date-probability", NO_DATE_PROBABILITY, [0.0, 0.5],
                          "an under-construction facility with no expected online date counts at this probability"),
    ]


def effective_supply(region_id: str, facilities: list[domain.Facility], as_of: datetime, *,
                     utilisation: float = DEFAULT_UTILISATION,
                     overrides: dict[str, tuple[float, str]] | None = None) -> domain.EffectiveSupply:
    """One region's estimate; `overrides` maps a facility id to (completion probability, reason) for scenarios."""
    if not UTILISATION_RANGE[0] <= utilisation <= UTILISATION_RANGE[1]:
        raise ValueError(f"utilisation {utilisation} outside the stated range {UTILISATION_RANGE}")
    overrides = overrides or {}
    rows, left_out, kept = one_class_facilities(facilities, region_id)
    operating = under = announced = reported = effective = 0.0
    gpu_effective = 0.0
    gpu_counted = gpu_total = 0
    breakdown: list[domain.FacilitySupply] = []
    for f in sorted(rows, key=lambda f: f.id):
        mw = f.capacity_mw or 0.0
        probability: float | None = None
        late: float | None = None
        contributes = 0.0
        if f.status == "operating":
            operating += mw
            contributes = mw * utilisation
            gpu_total += 1
            if f.gpu_count_estimate is not None:
                gpu_counted += 1
                gpu_effective += f.gpu_count_estimate * utilisation
        elif f.status == "under-construction":
            under += mw
            if f.id in overrides:
                probability, late = overrides[f.id][0], months_late(f.timeline.expected_online, as_of)
            else:
                probability, late = completion_probability(f.timeline.expected_online, as_of)
            contributes = mw * probability
            gpu_total += 1
            if f.gpu_count_estimate is not None:
                gpu_counted += 1
                gpu_effective += f.gpu_count_estimate * probability
        elif f.status in ANNOUNCED_STATUSES:
            announced += mw
            late = months_late(f.timeline.expected_online, as_of)
        else:
            reported += mw
        effective += contributes
        breakdown.append(domain.FacilitySupply(
            facility_id=f.id, name=f.name, status=f.status, capacity_mw=f.capacity_mw, gpu_count_estimate=f.gpu_count_estimate,
            expected_online=f.timeline.expected_online, months_late=late, completion_probability=probability,
            contributes_mw=round(contributes, 2),
        ))
    if gpu_total and gpu_counted == gpu_total:
        gpu_equivalents: int | None = int(round(gpu_effective))
        coverage = f"all {gpu_total} counted facilities carry a GPU estimate"
    else:
        gpu_equivalents = None
        coverage = f"{gpu_counted} of {gpu_total} counted facilities carry a GPU estimate; MW only"
    prov = derived(METHOD, [f.provenance for f in rows], observed_at=as_of,
                   note="facility records x stated assumptions; announced MW is never counted as operating" + class_note(kept, left_out))
    return domain.EffectiveSupply(
        region_id=region_id, as_of=as_of, operating_mw=round(operating, 2), under_construction_mw=round(under, 2),
        announced_mw=round(announced, 2), reported_mw=round(reported, 2), effective_mw=round(effective, 2),
        effective_gpu_equivalents=gpu_equivalents, gpu_equivalents_coverage=coverage, facilities=breakdown,
        assumptions=assumptions(utilisation), method=METHOD, input_kinds=list(prov.input_kinds), provenance=prov,
    )


def all_regions(regions: dict[str, domain.Region], facilities: list[domain.Facility], as_of: datetime, *,
                utilisation: float = DEFAULT_UTILISATION) -> list[domain.EffectiveSupply]:
    return [effective_supply(rid, facilities, as_of, utilisation=utilisation) for rid in sorted(regions)]
