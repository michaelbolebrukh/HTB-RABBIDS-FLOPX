"""Anomaly detection over the pressure grid (architecture §10.3): deterministic rules, stable ids.

| type                    | rule                                                                                   |
|-------------------------|----------------------------------------------------------------------------------------|
| bottleneck              | score >= 75 and availability normalised >= 0.5                                          |
| announcement-mismatch   | capacity-pipeline normalised <= -0.75 while score >= 60, or operating MW >= 200 while premium >= 0.2 |
| price-dispersion        | (p90 - p10) / median of the cell's comparable asks >= 0.6 with at least 4 asks           |
| overhang                | score <= 25 with capacity-pipeline normalised <= -0.5                                   |
| divergence              | the cell's 30-day index change and the linked basket's 30-day return differ in sign by more than 10 points |

The pipeline rules read the factor's normalised value, so they mean the same
whether the factor is a ratio to operating MW (normalised -0.5 is a 1.0x
pipeline, -0.75 a 1.5x one) or, with nothing operating, absolute MW (250 and
375 MW); a ratio over a 1 MW floor is never reported.

Ids are `anomaly:<type>:<regionId>:<gpuModelId>`. Severity is 1 + the band
distance (how many 25-point bands the score sits past the centre, or the rule's
own excess for the two non-score rules) + 1 when no factor argues against the
score, capped at 5; the basis text travels with the number. Cells with no asks
are skipped: a rule over an empty cell would be a statement about nothing.

`firstSeen` is never the time of the read. It is the earliest snapshot in the
cell's score history at which the rule held; for the two rules a score history
cannot answer (dispersion, divergence), and whenever an earlier detection is on
record, the time the caller first detected it (`first_detected`, which the
composition persists so it is stable across reads); otherwise the time of the
snapshot the rule was found in. `firstSeenBasis` says which of the three it is.

A factor is listed once per anomaly (R1-7): a factor the rule rests on is
supporting, and is left out of the contradicting list even when it pushes the
cell's score the other way (the announcement-mismatch pipeline does both).

The divergence basket is the cell's documented companies (an observed facility
record or an SEC filing) when any exist. Otherwise, by decision G11-b, it may be
the companies linked only by an authored sample profile or sample facility
record: the headline then says so ("linked by an authored sample profile, not a
filing"), `measures.basketBasis` reads "authored", and the links' own sample
provenance joins the anomaly's inputs so it carries the SAMPLE label.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from product.flopx import domain
from product.flopx.adjust import adjust_offer
from product.flopx.effective_supply import one_class_facilities
from product.flopx.provenance import Provenance, derived
from product.flopx.series import point_in_time

RULES = {
    "bottleneck": "score >= 75 and availability normalised >= 0.5",
    "announcement-mismatch": "capacity-pipeline normalised <= -0.75 (a pipeline of 1.5x operating MW, or 375 MW where nothing operates) while score >= 60, or operating MW >= 200 while price-premium raw >= 0.2",
    "price-dispersion": "(p90 - p10) / median of comparable asks >= 0.6 with at least 4 asks in the cell",
    "overhang": "score <= 25 with capacity-pipeline normalised <= -0.5 (a pipeline of 1.0x operating MW, or 250 MW where nothing operates)",
    "divergence": "30-day index change and the linked basket's 30-day return differ in sign by more than 10 percentage points; the basket is the documented companies, else those on authored sample facility records in the region, else the authored profile links, labelled as such",
}
DISPERSION_THRESHOLD = 0.6
DISPERSION_MIN_OFFERS = 4
DIVERGENCE_GAP = 0.10
MISMATCH_PIPELINE = -0.75
OVERHANG_PIPELINE = -0.5


def pipeline_text(pipeline: domain.Factor | None) -> str:
    """The pipeline as a reader can check it: a ratio to operating capacity, or MW against none."""
    if pipeline is None or pipeline.raw is None:
        return "no pipeline data"
    if pipeline.unit == "MW":
        return f"a capacity pipeline of {pipeline.raw:,.0f} MW, with no operating capacity in the data"
    return f"a capacity pipeline of {pipeline.raw:.2f}x operating capacity"
BASKET_DAYS = 30


def authored_basis_text(links: list[domain.Exposure]) -> str:
    """How undocumented links reach a cell, in words: the authored profile, authored facility records, or both."""
    bases = {b for e in links for b in e.basis}
    facility = bool(bases & {"facility-owner", "facility-operator"})
    profile = "sample-profile" in bases
    if facility and profile:
        return "linked by authored sample facility records and an authored sample profile"
    if facility:
        return "linked by authored sample facility records"
    return "linked by an authored sample profile"


FACILITY_BASES = ("facility-owner", "facility-operator")


def divergence_basket(exposures: list[domain.Exposure]) -> list[domain.Exposure]:
    """The companies a divergence compares against, most specific basis first: the documented links; else the
    links resting on an authored sample facility record in the region; else the authored profile links (G11-b).
    A profile tag reaches every cell alike, so the facility links are preferred while any exist."""
    documented = [e for e in exposures if e.documented]
    if documented:
        return documented
    facility = [e for e in exposures if any(b in FACILITY_BASES for b in e.basis)]
    return facility or list(exposures)


def _factor(cell: domain.PressureCell, name: str) -> domain.Factor | None:
    return next((f for f in cell.factors if f.factor == name), None)


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between order statistics (the same definition a port can reproduce)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def basket_return(tickers: list[str], series: dict[str, domain.Series], at: datetime, days: int = BASKET_DAYS) -> tuple[float | None, list[str]]:
    """Equal-weight mean of each ticker's close change over `days`, using only points known at `at`."""
    returns, used = [], []
    for t in tickers:
        s = series.get(f"equity:{t}:close")
        if s is None:
            continue
        points = point_in_time(s, at)
        if len(points) < 2:
            continue
        target = points[-1].t - timedelta(days=days)
        earlier = None
        for p in points:
            if p.t <= target:
                earlier = p
            else:
                break
        if earlier is None or earlier.value == 0:
            continue
        returns.append(points[-1].value / earlier.value - 1.0)
        used.append(t)
    return (statistics.fmean(returns) if returns else None), used


def _severity(band_distance: int, contradiction_free: bool) -> tuple[int, str]:
    severity = min(5, max(1, 1 + band_distance + (1 if contradiction_free else 0)))
    basis = f"1 + band distance {band_distance}" + (" + 1 (no factor argues against the score)" if contradiction_free else " + 0 (contradicted)")
    return severity, basis


FIRST_SEEN_HISTORY = "the earliest snapshot in the cell's score history at which the rule held"
FIRST_SEEN_DETECTED = "first detected at this time; no earlier snapshot on record shows the rule holding"
FIRST_SEEN_SNAPSHOT = "the snapshot the rule was found in; no earlier snapshot on record shows it holding"


def snapshot_time(cell: domain.PressureCell) -> datetime:
    """The time of the snapshot the cell was computed from: its newest history point, else its as-of."""
    return cell.history[-1].t if cell.history else cell.as_of


def _first_seen(cell: domain.PressureCell, holds, detected: datetime | None) -> tuple[datetime, str]:
    """(firstSeen, basis): see the module docstring."""
    current = snapshot_time(cell)
    held = None
    if holds is not None:
        held = next((point.t for point in cell.history if holds(point)), None)
    if held is not None and held < current and (detected is None or held <= detected):
        return held, FIRST_SEEN_HISTORY
    if detected is not None and detected < current:
        return detected, FIRST_SEEN_DETECTED
    return current, FIRST_SEEN_SNAPSHOT


def _anomaly(cell: domain.PressureCell, kind: str, headline: str, measures: dict, supporting: list[domain.Factor],
             band_distance: int, exposures: list[domain.Exposure], seeds: list[str], holds_at,
             first_detected: dict[str, datetime], extra_inputs: list[Provenance] = ()) -> domain.Anomaly:
    severity, basis = _severity(band_distance, not cell.contradictions)
    anomaly_id = f"anomaly:{kind}:{cell.region_id}:{cell.gpu_model_id}"
    first_seen, first_seen_basis = _first_seen(cell, holds_at, first_detected.get(anomaly_id))
    prov = derived(f"anomaly:{kind}", [cell.provenance, *extra_inputs], observed_at=cell.provenance.observed_at, note=RULES[kind])
    # One list per factor (R1-7): what the rule rests on is supporting, never also contradicting.
    supporting_names = {f.factor for f in supporting}
    contradicting = [f for f in cell.contradictions if f.factor not in supporting_names]
    return domain.Anomaly(
        id=anomaly_id, type=kind, region_id=cell.region_id, gpu_model_id=cell.gpu_model_id,
        severity=severity, headline=headline, rule=RULES[kind], measures=measures, supporting=supporting,
        contradicting=contradicting, uncertainty=cell.uncertainty, first_seen=first_seen,
        history=list(cell.history), linked_companies=exposures, scenario_seeds=seeds, provenance=prov, severity_basis=basis,
        first_seen_basis=first_seen_basis,
    )


def scenario_seeds(region_id: str, facilities: list[domain.Facility]) -> list[str]:
    seeds = [f"delay-facility:{f.id}" for f in facilities if f.region_id == region_id and f.status == "under-construction"]
    seeds += [f"complete-facility:{f.id}" for f in facilities if f.region_id == region_id and f.status == "under-construction"]
    return seeds[:4] + ["energy-shock", "demand-shock", "utilisation"]


def detect(
    cells: list[domain.PressureCell],
    *,
    offers: list[domain.GpuOffer],
    gpu_models: dict[str, domain.GpuModel],
    regions: dict[str, domain.Region],
    facilities: list[domain.Facility],
    series: dict[str, domain.Series],
    exposures_by_region: dict[str, list[domain.Exposure]],
    first_detected: dict[str, datetime] | None = None,
) -> list[domain.Anomaly]:
    """Every rule over every cell with asks. `first_detected` maps an anomaly id to the earliest time the
    caller has on record detecting it (empty or None when nothing is persisted)."""
    first_detected = first_detected or {}
    out: list[domain.Anomaly] = []
    for cell in cells:
        if cell.uncertainty.coverage == 0:
            continue
        region = regions.get(cell.region_id)
        place = region.name if region else cell.region_id
        model = cell.gpu_model_id
        exposures = exposures_by_region.get(cell.region_id, [])
        seeds = scenario_seeds(cell.region_id, facilities)
        avail = _factor(cell, "availability")
        pipeline = _factor(cell, "capacity-pipeline")
        premium = _factor(cell, "price-premium")
        momentum = _factor(cell, "price-momentum")
        band_distance = int(abs(cell.score - 50.0) // 25)
        operating = sum(f.capacity_mw or 0.0 for f in one_class_facilities(facilities, cell.region_id)[0] if f.status == "operating")

        if cell.score >= 75 and avail and avail.normalised is not None and avail.normalised >= 0.5:
            out.append(_anomaly(
                cell, "bottleneck",
                f"{model} in {place}: acute inferred pressure ({cell.score:.0f}) with {avail.raw:.0%} of asks unavailable or limited",
                {"score": cell.score, "availabilityNormalised": avail.normalised, "availabilityRaw": avail.raw},
                [f for f in (avail, premium, momentum) if f and f.normalised is not None and f.normalised > 0], band_distance, exposures, seeds,
                lambda p: p.score >= 75, first_detected))

        pipeline_raw = pipeline.raw if pipeline and pipeline.raw is not None else None
        pipeline_unit = pipeline.unit if pipeline and pipeline.raw is not None else None
        pipeline_norm = pipeline.normalised if pipeline and pipeline.normalised is not None else None
        premium_raw = premium.raw if premium and premium.raw is not None else None
        pipeline_clause = pipeline_norm is not None and pipeline_norm <= MISMATCH_PIPELINE and cell.score >= 60
        if pipeline_clause or (operating >= 200 and premium_raw is not None and premium_raw >= 0.2):
            clause = ("large announced pipeline while prices and scarcity rise anyway" if pipeline_clause
                      else "substantial operating capacity while asks carry a premium")
            out.append(_anomaly(
                cell, "announcement-mismatch",
                f"{model} in {place}: {clause} ({pipeline_text(pipeline)}; premium {(premium_raw or 0):+.0%})",
                {"score": cell.score, "pipelineRaw": pipeline_raw, "pipelineUnit": pipeline_unit, "operatingMw": operating, "premiumRaw": premium_raw},
                [f for f in (pipeline, premium) if f and f.normalised is not None], band_distance, exposures, seeds,
                lambda p: p.score >= 60, first_detected))

        cell_offers = [o for o in offers if o.gpu_model_id == model and o.region_id == cell.region_id]
        if len(cell_offers) >= DISPERSION_MIN_OFFERS:
            comparable = [adjust_offer(o, gpu_models[model], regions.get(o.region_id)).adjusted for o in cell_offers]
            median = statistics.median(comparable)
            spread = (percentile(comparable, 0.9) - percentile(comparable, 0.1)) / median if median > 0 else 0.0
            if spread >= DISPERSION_THRESHOLD:
                excess = min(2, int((spread - DISPERSION_THRESHOLD) / 0.3))
                out.append(_anomaly(
                    cell, "price-dispersion",
                    f"{model} in {place}: comparable asks spread {spread:.0%} of the median across {len(cell_offers)} offers",
                    {"spread": round(spread, 4), "p10": round(percentile(comparable, 0.1), 4), "p90": round(percentile(comparable, 0.9), 4),
                     "median": round(median, 4), "offers": len(cell_offers)},
                    [f for f in (premium, avail) if f and f.normalised is not None], excess, exposures, seeds, None, first_detected))

        if cell.score <= 25 and pipeline_norm is not None and pipeline_norm <= OVERHANG_PIPELINE:
            out.append(_anomaly(
                cell, "overhang",
                f"{model} in {place}: slack inferred pressure ({cell.score:.0f}) while {pipeline_text(pipeline)}, is still coming" if pipeline_unit == "MW"
                else f"{model} in {place}: slack inferred pressure ({cell.score:.0f}) while {pipeline_text(pipeline)} is still coming",
                {"score": cell.score, "pipelineRaw": pipeline_raw, "pipelineUnit": pipeline_unit},
                [f for f in (pipeline, premium, avail) if f and f.normalised is not None and f.normalised < 0], band_distance, exposures, seeds,
                lambda p: p.score <= 25, first_detected))

        linked = divergence_basket(exposures)
        documented = [e for e in linked if e.documented]
        if momentum and momentum.raw is not None and linked:
            basket, used = basket_return([e.ticker for e in linked], series, cell.as_of)
            if basket is not None and (momentum.raw > 0) != (basket > 0) and abs(momentum.raw - basket) > DIVERGENCE_GAP:
                gap = abs(momentum.raw - basket)
                used_links = [e for e in linked if e.ticker in used]
                if documented:
                    basket_text, basket_basis, extra = f"the documented basket ({', '.join(used)})", "documented", []
                else:
                    basket_text = f"the basket {authored_basis_text(used_links)}, not a filing ({', '.join(used)}),"
                    basket_basis, extra = "authored", [e.provenance for e in used_links]
                # The momentum factor reads a regional series only where one exists; otherwise it is the
                # model's global series, and the headline must say so rather than read as a local move.
                regional_series = f"gpu:{cell.gpu_model_id}:on-demand:{cell.region_id}" in series
                moved = "the regional price moved" if regional_series else f"the global {model} index (no regional series) moved"
                out.append(_anomaly(
                    cell, "divergence",
                    f"{model} in {place}: {moved} {momentum.raw:+.1%} over 30 days while {basket_text} moved {basket:+.1%}",
                    {"indexChange30d": round(momentum.raw, 4), "basketReturn30d": round(basket, 4), "gap": round(gap, 4), "basket": used,
                     "basketBasis": basket_basis},
                    [momentum], min(2, int(gap / DIVERGENCE_GAP) - 1), exposures, seeds, None, first_detected, extra))
    out.sort(key=lambda a: (-a.severity, a.id))
    return out
