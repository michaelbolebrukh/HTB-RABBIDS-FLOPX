"""The Compute Pressure Index, `cpx-v1` (architecture §10.2): an INFERENCE, never a measurement.

One cell is (regionId, gpuModelId) at a date. Six factors, each normalised to
[-1, 1] with a stated weight and its evidence; a factor with no data is listed
with `normalised: None` and left out of the weighted sum (the weights of the
factors with data are renormalised, and coverage drops). The score is
`50 + 50 * sum(w_i * n_i) / sum(w_i)` over the factors with data, banded
slack (<25), balanced (25-50), tight (50-75), acute (>=75).

| factor            | raw                                                          | normalised                 | weight |
|-------------------|--------------------------------------------------------------|----------------------------|--------|
| price-premium     | cell median comparable ask / model global median - 1         | clamp(raw / 0.30, -1, 1)   | 0.30   |
| availability      | share unavailable + 0.5 x share limited                      | 2 x raw - 1                | 0.20   |
| price-momentum    | 30-day change of the model's index (regional, else global;   | clamp(raw / 0.15, -1, 1)   | 0.15   |
|                   | else GetDeploying's weekly median while the index is short)  |                            |        |
| power-stress      | zone's latest energy price / trailing 30-day median - 1      | clamp(raw / 0.50, -1, 1)   | 0.15   |
| capacity-pipeline | (announced + planned + under-construction MW) / operating MW | -clamp(raw / 2.0, -1, 1)   | 0.10   |
|                   | with no operating MW: the pipeline MW itself (unit MW)       | -clamp(raw / 500, -1, 1)   |        |
| demand-signals    | mean(market price-up probability centred x2, feed intensity) | the mean itself            | 0.10   |

`uncertainty.label` is `exploratory` (coverage < 3 or fewer than 4 factors with
data or more than half the inputs sample) or `indicative`; it is never
`measured`, because nothing here measures utilisation. `contradictions` lists
every factor whose direction opposes the score's side of 50, with its evidence.

History is honest about its thinness: the only regional offer history the
sample set has is two snapshots a week apart, and live asks accumulate in the
archive from first deploy. `cell_history()` recomputes the score at each
snapshot date from the data known at that date and states the basis.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

from product.flopx import domain
from product.flopx.adjust import adjust_offer
from product.flopx.effective_supply import class_note, one_class_facilities
from product.flopx.provenance import Provenance, derived, effective_kind
from product.flopx.series import point_in_time

METHOD = "cpx-v1"
METHOD_TEXT = (
    "Compute Pressure Index cpx-v1: a weighted sum of six normalised factors (comparable-price premium, "
    "advertised availability, 30-day index momentum, power-price stress, facility pipeline relief and demand "
    "signals) on a 0-100 scale. It infers pressure on usable supply from advertised asks, facility records, "
    "power prices, prediction markets and the feed; it does not measure utilisation and nothing in it is "
    "observed ground truth."
)

WEIGHTS = {
    "price-premium": 0.30,
    "availability": 0.20,
    "price-momentum": 0.15,
    "power-stress": 0.15,
    "capacity-pipeline": 0.10,
    "demand-signals": 0.10,
}
FACTORS = tuple(WEIGHTS)
# Band edges: slack below 25, balanced up to and including 50 (the no-information centre), tight below 75, acute from 75:
BANDS = (("slack", 25.0), ("balanced", 50.0), ("tight", 75.0))

PREMIUM_SCALE = 0.30
MOMENTUM_SCALE = 0.15
POWER_SCALE = 0.50
PIPELINE_SCALE = 2.0
# With nothing operating there is no ratio (a floor of 1 MW once turned 50 MW into "50x"); the pipeline is
# then scored on an absolute scale: this many MW coming reads as full relief, the ratio scale's 2x.
PIPELINE_ABSOLUTE_MW = 500.0
MOMENTUM_DAYS = 30
# N-GDH-BE: while FLOPX's own index is younger than the momentum window, the factor may read GetDeploying's weekly median
# (archived by tools/getdeploying_load.py, CC BY 4.0), labelled with this scope so no reader mistakes it for FLOPX history.
GETDEPLOYING_SCOPE = "getdeploying-weekly"
GETDEPLOYING_LICENCE = "CC BY 4.0"
POWER_WINDOW_DAYS = 30
POWER_MIN_POINTS = 5
DEMAND_WINDOW_DAYS = 30
FEED_SATURATION = 5.0
PIPELINE_STATUSES = ("announced", "planned", "under-construction")

MIN_COVERAGE_INDICATIVE = 3
MIN_FACTORS_INDICATIVE = 4
MAX_SAMPLE_SHARE_INDICATIVE = 0.5

# Words that mark a prediction-market question as a price-UP or price-DOWN bracket for a GPU model:
UP_WORDS = ("above", "over", "exceed", "exceeds", "higher")
DOWN_WORDS = ("below", "under", "lower")
GPU_WORDS = {
    "h100-sxm-80gb": ("h100",), "h200-sxm-141gb": ("h200",), "b200-sxm-192gb": ("b200", "blackwell"),
    "a100-sxm-80gb": ("a100",), "l40s-48gb": ("l40s", "l40"), "rtx-4090-24gb": ("4090",),
}


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def band_for(score: float) -> str:
    if score < 25.0:
        return "slack"
    if score <= 50.0:
        return "balanced"
    if score < 75.0:
        return "tight"
    return "acute"


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _direction(normalised: float | None) -> str:
    if normalised is None:
        return "none"
    if normalised > 0:
        return "up"
    if normalised < 0:
        return "down"
    return "flat"


def _factor(name: str, raw: float | None, unit: str, normalised: float | None, evidence: list[Provenance], note: str,
            observed_at: datetime | None = None, scope: str | None = None) -> domain.Factor:
    weight = WEIGHTS[name]
    normalised = _round(normalised)
    if observed_at is None and evidence:
        observed_at = max(p.observed_at for p in evidence)
    return domain.Factor(
        factor=name, raw=_round(raw), unit=unit, normalised=normalised, weight=weight,
        contribution=_round(weight * normalised) if normalised is not None else None,
        direction=_direction(normalised), evidence=list(evidence), observed_at=observed_at, note=note, scope=scope,
    )


# -- the six factors -----------------------------------------------------------------

def _comparable(offers: list[domain.GpuOffer], gpu_model: domain.GpuModel, regions: dict[str, domain.Region]) -> list[float]:
    return [adjust_offer(o, gpu_model, regions.get(o.region_id)).adjusted for o in offers]


def price_premium(cell_offers: list[domain.GpuOffer], model_offers: list[domain.GpuOffer], gpu_model: domain.GpuModel,
                  regions: dict[str, domain.Region]) -> domain.Factor:
    if not cell_offers or not model_offers:
        return _factor("price-premium", None, "ratio", None, [], "no asks in the cell: no premium can be read")
    cell_median = statistics.median(_comparable(cell_offers, gpu_model, regions))
    global_median = statistics.median(_comparable(model_offers, gpu_model, regions))
    if global_median <= 0:
        return _factor("price-premium", None, "ratio", None, [], "global median is zero")
    raw = cell_median / global_median - 1.0
    note = (f"cell median comparable ask {cell_median:.4f} vs model global median {global_median:.4f} USD/GPU-hour "
            f"over {len(cell_offers)} of {len(model_offers)} advertised asks; asks, not transactions")
    return _factor("price-premium", raw, "ratio", clamp(raw / PREMIUM_SCALE), [o.provenance for o in cell_offers], note)


NO_REGIONAL_ASKS = "n/a (no regional asks)"


def rental_read(cell_offers: list[domain.GpuOffer], model_offers: list[domain.GpuOffer], gpu_model: domain.GpuModel,
                regions: dict[str, domain.Region], series: dict[str, domain.Series], region_id: str,
                as_of: datetime | None = None) -> domain.RentalRead:
    """The cell's rental economics for the X-Ray (N-XRAY). With regional asks: their comparable median and the
    premium over the model's global median, the same numbers the price-premium factor reads. Without them: the
    global median of the model's advertised asks (live when the caller passed live offers, sample otherwise) and
    GetDeploying's archived weekly median for the model, labelled as the fallback, with the premium `None` and
    `premium_label` saying `n/a (no regional asks)`, never an empty tab. Asks, never transactions."""
    region_name = regions[region_id].name if region_id in regions else region_id
    weekly = series.get(f"gpu:{gpu_model.id}:getdeploying:weekly-median")
    weekly_points = (point_in_time(weekly, as_of) if as_of is not None else weekly.points) if weekly is not None else []
    weekly_last = weekly_points[-1] if weekly_points else None
    global_median = statistics.median(_comparable(model_offers, gpu_model, regions)) if model_offers else None
    if cell_offers and global_median:
        cell_median = statistics.median(_comparable(cell_offers, gpu_model, regions))
        premium = cell_median / global_median - 1.0
        evidence = [o.provenance for o in cell_offers]
        sources = sorted({o.provenance.source for o in cell_offers})
        return domain.RentalRead(
            basis="regional-asks", median_usd_per_gpu_hour=_round(cell_median), premium_pct=_round(premium * 100, 1),
            premium_label=f"{premium * 100:+.1f}%", ask_count=len(cell_offers),
            global_median_usd_per_gpu_hour=_round(global_median), global_ask_count=len(model_offers),
            weekly_median_usd_per_gpu_hour=_round(weekly_last.value) if weekly_last else None,
            weekly_week_ending=weekly_last.t if weekly_last else None, source=", ".join(sources),
            note=(f"median comparable ask over {len(cell_offers)} advertised asks in {region_name} vs the model's global "
                  f"median over {len(model_offers)}; asks, not transactions"),
            evidence=evidence, observed_at=max(p.observed_at for p in evidence),
        )
    evidence = [o.provenance for o in model_offers] + ([weekly_last.provenance] if weekly_last else [])
    if global_median is None and weekly_last is None:
        return domain.RentalRead(
            basis="none", median_usd_per_gpu_hour=None, premium_pct=None, premium_label=NO_REGIONAL_ASKS, ask_count=0,
            global_median_usd_per_gpu_hour=None, global_ask_count=0, weekly_median_usd_per_gpu_hour=None,
            weekly_week_ending=None, source="none", note=f"no advertised asks for {gpu_model.id} anywhere and no archived weekly median",
            evidence=[], observed_at=None,
        )
    sources = sorted({o.provenance.source for o in model_offers}) + ([f"getdeploying weekly median ({GETDEPLOYING_LICENCE}, archived)"] if weekly_last else [])
    parts = []
    if global_median is not None:
        parts.append(f"the model's global median comparable ask {global_median:.4f} USD/GPU-hour over {len(model_offers)} advertised asks")
    if weekly_last is not None:
        parts.append(f"GetDeploying's archived weekly median {weekly_last.value:.4f} (week ending {weekly_last.t.date().isoformat()})")
    return domain.RentalRead(
        basis="global-fallback",
        median_usd_per_gpu_hour=_round(global_median if global_median is not None else weekly_last.value),
        premium_pct=None, premium_label=NO_REGIONAL_ASKS, ask_count=0,
        global_median_usd_per_gpu_hour=_round(global_median), global_ask_count=len(model_offers),
        weekly_median_usd_per_gpu_hour=_round(weekly_last.value) if weekly_last else None,
        weekly_week_ending=weekly_last.t if weekly_last else None, source=", ".join(sources),
        note=f"fallback: no live asks for {gpu_model.id} in {region_name}; shown instead: " + " and ".join(parts) + "; no regional premium can be read",
        evidence=evidence, observed_at=max((p.observed_at for p in evidence), default=None),
    )


def availability(cell_offers: list[domain.GpuOffer]) -> domain.Factor:
    if not cell_offers:
        return _factor("availability", None, "share", None, [], "no asks in the cell")
    n = len(cell_offers)
    unavailable = sum(o.availability == "unavailable" for o in cell_offers) / n
    limited = sum(o.availability == "limited" for o in cell_offers) / n
    raw = unavailable + 0.5 * limited
    note = f"{n} asks: {unavailable:.0%} unavailable, {limited:.0%} limited; advertised availability, not utilisation"
    return _factor("availability", raw, "share", 2.0 * raw - 1.0, [o.provenance for o in cell_offers], note)


def _change_over(points: list[domain.PriceObservation], days: int) -> tuple[float | None, domain.PriceObservation | None]:
    if len(points) < 2:
        return None, None
    last = points[-1]
    target = last.t - timedelta(days=days)
    earlier = None
    for p in points:
        if p.t <= target:
            earlier = p
        else:
            break
    if earlier is None or earlier.value == 0:
        return None, None
    return last.value / earlier.value - 1.0, earlier


def _spans(points: list[domain.PriceObservation], days: int) -> bool:
    """True when the points reach back at least `days` before their last one: what `_change_over` needs to read a change."""
    return len(points) >= 2 and points[-1].t - points[0].t >= timedelta(days=days)


def _history_days(points: list[domain.PriceObservation]) -> int:
    return len({p.t.date() for p in points})


def momentum_series_for(series: dict[str, domain.Series], gpu_model_id: str, region_id: str, as_of: datetime | None = None, *,
                        weekly_fallback: bool = True) -> tuple[domain.Series | None, str | None]:
    """The one series the momentum factor reads, and its scope.

    Regional on-demand series when one exists (GetDeploying is global, so this branch never changes); else the
    model's FLOPX index when the points known at `as_of` span the 30-day window; else, when the archive holds
    GetDeploying's weekly median for the model spanning that window, that series with scope "getdeploying-weekly"
    (N-GDH-BE: a labelled stand-in while FLOPX's own history is too short, never merged with it); else the index as
    it stands, short; else the global sample series. Without `as_of` the whole series is judged. `weekly_fallback=False`
    is the chain as it was before N-GDH-BE, for a caller that needs daily points to project from rather than a change.
    """
    def known(s: domain.Series) -> list[domain.PriceObservation]:
        return s.points if as_of is None else point_in_time(s, as_of)

    regional = series.get(f"gpu:{gpu_model_id}:on-demand:{region_id}")
    if regional is not None and regional.points:
        return regional, None
    index = series.get(f"index:fcpi:{gpu_model_id}")
    if index is not None and index.points and _spans(known(index), MOMENTUM_DAYS):
        return index, "global"
    if weekly_fallback:
        weekly = series.get(f"gpu:{gpu_model_id}:getdeploying:weekly-median")
        if weekly is not None and _spans(known(weekly), MOMENTUM_DAYS):
            return weekly, GETDEPLOYING_SCOPE
    if index is not None and index.points:
        return index, "global"
    global_sample = series.get(f"gpu:{gpu_model_id}:on-demand:global")
    if global_sample is not None and global_sample.points:
        return global_sample, "global"
    return None, None


def price_momentum(series: dict[str, domain.Series], gpu_model_id: str, region_id: str, as_of: datetime) -> domain.Factor:
    s, scope = momentum_series_for(series, gpu_model_id, region_id, as_of)
    if s is None:
        return _factor("price-momentum", None, "ratio", None, [], "no index series for the model")
    points = point_in_time(s, as_of)
    raw, earlier = _change_over(points, MOMENTUM_DAYS)
    if raw is None:
        return _factor("price-momentum", None, "ratio", None, [p.provenance for p in points[-1:]],
                       f"{s.id} has fewer than {MOMENTUM_DAYS} days of history before {as_of.date().isoformat()}", scope=scope)
    if scope == GETDEPLOYING_SCOPE:
        # The evidence is the two GetDeploying points themselves (archived, CC BY 4.0), so the cell's input kinds and
        # sample share say what was read. Weekly points sit 35 days apart across a 30-day window, so the gap is stated.
        index = series.get(f"index:fcpi:{gpu_model_id}")
        own_days = _history_days(point_in_time(index, as_of)) if index is not None else 0
        note = (f"{MOMENTUM_DAYS}-day change of GetDeploying's weekly median ({GETDEPLOYING_LICENCE}), FLOPX index history too short "
                f"({own_days} days); {s.id}: {points[-1].value:.4f} (week ending {points[-1].t.date().isoformat()}) vs "
                f"{earlier.value:.4f} (week ending {earlier.t.date().isoformat()}), {(points[-1].t - earlier.t).days} days apart")
    else:
        note = f"{s.id}: {points[-1].value:.4f} vs {earlier.value:.4f} {MOMENTUM_DAYS} days earlier" + (" (global series, no regional one exists)" if scope else "")
    return _factor("price-momentum", raw, "ratio", clamp(raw / MOMENTUM_SCALE), [earlier.provenance, points[-1].provenance], note,
                   observed_at=points[-1].t, scope=scope)


def power_stress(series: dict[str, domain.Series], region: domain.Region | None, as_of: datetime) -> domain.Factor:
    zone = region.grid_zone if region else None
    s = series.get(f"energy:{zone}:day-ahead") if zone else None
    if s is None:
        return _factor("power-stress", None, "ratio", None, [], f"no day-ahead energy series for grid zone {zone or 'unknown'}")
    points = point_in_time(s, as_of)
    if not points:
        return _factor("power-stress", None, "ratio", None, [], f"{s.id} has no points before {as_of.date().isoformat()}")
    last = points[-1]
    window = [p for p in points if p.t >= last.t - timedelta(days=POWER_WINDOW_DAYS)]
    if len(window) < POWER_MIN_POINTS:
        return _factor("power-stress", None, "ratio", None, [last.provenance], f"{s.id}: fewer than {POWER_MIN_POINTS} points in the trailing {POWER_WINDOW_DAYS} days")
    median = statistics.median(p.value for p in window)
    if median <= 0:
        return _factor("power-stress", None, "ratio", None, [last.provenance], "trailing median is zero")
    raw = last.value / median - 1.0
    note = f"{s.id}: latest {last.value:.2f} vs trailing {POWER_WINDOW_DAYS}-day median {median:.2f} {s.unit} over {len(window)} points"
    return _factor("power-stress", raw, "ratio", clamp(raw / POWER_SCALE), [last.provenance], note, observed_at=last.t)


def capacity_pipeline(facilities: list[domain.Facility], region_id: str) -> domain.Factor:
    rows, left_out, kept = one_class_facilities(facilities, region_id)
    if not rows:
        return _factor("capacity-pipeline", None, "ratio", None, [], "no facility records for the region")
    operating = sum(f.capacity_mw or 0.0 for f in rows if f.status == "operating")
    pipeline = sum(f.capacity_mw or 0.0 for f in rows if f.status in PIPELINE_STATUSES)
    tail = (f" ({len(rows)} facility records); a large pipeline is relief ahead, so the sign is inverted; "
            "reported-only facilities are not counted on either side" + class_note(kept, left_out))
    if operating <= 0:
        note = (f"{pipeline:.0f} MW announced, planned or under construction against no operating capacity in the data, "
                f"so there is no ratio: scored on an absolute scale where {PIPELINE_ABSOLUTE_MW:.0f} MW reads as full relief" + tail)
        return _factor("capacity-pipeline", pipeline, "MW", -clamp(pipeline / PIPELINE_ABSOLUTE_MW), [f.provenance for f in rows], note)
    raw = pipeline / operating
    note = f"{pipeline:.0f} MW announced, planned or under construction against {operating:.0f} MW operating" + tail
    return _factor("capacity-pipeline", raw, "ratio", -clamp(raw / PIPELINE_SCALE), [f.provenance for f in rows], note)


def _market_prob_at(market: domain.PredictionMarket, at: datetime) -> tuple[float | None, Provenance | None, datetime | None]:
    if market.last_updated <= at and market.outcomes:
        return market.outcomes[0].implied_probability, market.provenance, market.last_updated
    known = [p for p in market.history if p.t <= at]
    if known:
        return known[-1].value, known[-1].provenance, known[-1].t
    return None, None, None


def price_up_probability(market: domain.PredictionMarket, gpu_model_id: str, at: datetime) -> tuple[float | None, Provenance | None, datetime | None]:
    """The probability the market assigns to the model's price going UP, or None when the market is not a price bracket for it."""
    text = market.question.lower()
    words = GPU_WORDS.get(gpu_model_id, (gpu_model_id,))
    if not any(re.search(rf"\b{re.escape(w)}\b", text) for w in words) or not re.search(r"price|\$|/gpu|per gpu|discount", text):
        return None, None, None
    direction = 0
    if any(w in text for w in UP_WORDS):
        direction = 1
    elif any(w in text for w in DOWN_WORDS):
        direction = -1
    if direction == 0:
        return None, None, None
    p, prov, t = _market_prob_at(market, at)
    if p is None:
        return None, None, None
    return (p if direction > 0 else 1.0 - p), prov, t


def demand_signals(markets: list[domain.PredictionMarket], feed: list[domain.IntelItem], gpu_model_id: str, region_id: str,
                   as_of: datetime) -> domain.Factor:
    evidence: list[Provenance] = []
    parts: list[float] = []
    notes: list[str] = []
    market_values = []
    for m in markets:
        p, prov, t = price_up_probability(m, gpu_model_id, as_of)
        if p is not None:
            market_values.append(p)
            evidence.append(prov)
            notes.append(f"{m.id} implies {p:.3f} price-up probability")
    if market_values:
        parts.append(clamp((statistics.fmean(market_values) - 0.5) * 2.0))
    since = as_of - timedelta(days=DEMAND_WINDOW_DAYS)
    items = [i for i in feed if since <= i.observed_at <= as_of
             and (region_id in i.geography.region_ids or gpu_model_id in i.entities.gpu_model_ids)]
    if feed:
        weighted = sum(i.severity / 5.0 for i in items)
        parts.append(clamp(weighted / FEED_SATURATION, 0.0, 1.0))
        evidence.extend(i.provenance for i in items)
        notes.append(f"{len(items)} feed items touching the region or model in {DEMAND_WINDOW_DAYS} days (severity-weighted {weighted:.2f})")
    if not parts:
        return _factor("demand-signals", None, "score", None, [], "no price-bracket market for the model and no feed")
    raw = statistics.fmean(parts)
    observed = max((p.observed_at for p in evidence), default=None)
    return _factor("demand-signals", raw, "score", raw, evidence, "; ".join(notes) or "feed only", observed_at=observed)


# -- the cell --------------------------------------------------------------------------

def uncertainty_of(coverage: int, factors: list[domain.Factor]) -> domain.Uncertainty:
    with_data = [f for f in factors if f.normalised is not None]
    evidence = [p for f in factors for p in f.evidence]
    sample_share = (sum(effective_kind(p) == "sample" for p in evidence) / len(evidence)) if evidence else 0.0
    oldest = min((p.observed_at for p in evidence), default=None)
    label = "indicative"
    if coverage < MIN_COVERAGE_INDICATIVE or len(with_data) < MIN_FACTORS_INDICATIVE or sample_share > MAX_SAMPLE_SHARE_INDICATIVE:
        label = "exploratory"
    return domain.Uncertainty(coverage=coverage, factors_with_data=len(with_data), factors_total=len(FACTORS),
                              sample_share=round(sample_share, 4), oldest_input=oldest, label=label)


def score_of(factors: list[domain.Factor]) -> float | None:
    with_data = [f for f in factors if f.normalised is not None]
    if not with_data:
        return None
    total_weight = sum(f.weight for f in with_data)
    return round(50.0 + 50.0 * sum(f.weight * f.normalised for f in with_data) / total_weight, 2)


def contradictions_of(score: float, factors: list[domain.Factor]) -> list[domain.Factor]:
    if score > 50:
        return [f for f in factors if f.normalised is not None and f.normalised < 0]
    if score < 50:
        return [f for f in factors if f.normalised is not None and f.normalised > 0]
    return []


def compute_cell(
    region_id: str,
    gpu_model_id: str,
    *,
    offers: list[domain.GpuOffer],
    gpu_models: dict[str, domain.GpuModel],
    regions: dict[str, domain.Region],
    series: dict[str, domain.Series],
    facilities: list[domain.Facility],
    markets: list[domain.PredictionMarket],
    feed: list[domain.IntelItem],
    as_of: datetime,
) -> domain.PressureCell:
    """One cell's score, factors, contradictions and uncertainty from the data known at `as_of`.

    `offers` must be one provenance class (the caller picks live or sample per
    model, never both); the cell's provenance records the kinds it saw.
    """
    gpu_model = gpu_models[gpu_model_id]
    model_offers = [o for o in offers if o.gpu_model_id == gpu_model_id]
    cell_offers = [o for o in model_offers if o.region_id == region_id]
    factors = [
        price_premium(cell_offers, model_offers, gpu_model, regions),
        availability(cell_offers),
        price_momentum(series, gpu_model_id, region_id, as_of),
        power_stress(series, regions.get(region_id), as_of),
        capacity_pipeline(facilities, region_id),
        demand_signals(markets, feed, gpu_model_id, region_id, as_of),
    ]
    score = score_of(factors)
    if score is None:
        score = 50.0
    uncertainty = uncertainty_of(len(cell_offers), factors)
    evidence = [p for f in factors for p in f.evidence]
    prov = derived(METHOD, evidence, observed_at=max((p.observed_at for p in evidence), default=as_of),
                   note="inferred pressure on usable supply; not a measurement of utilisation")
    return domain.PressureCell(
        region_id=region_id, gpu_model_id=gpu_model_id, as_of=as_of, score=score, band=band_for(score),
        factors=factors, contradictions=contradictions_of(score, factors), uncertainty=uncertainty,
        method=METHOD, method_text=METHOD_TEXT, input_kinds=list(prov.input_kinds), provenance=prov,
        rental=rental_read(cell_offers, model_offers, gpu_model, regions, series, region_id, as_of),
    )


def compute_grid(*, offers, gpu_models, regions, series, facilities, markets, feed, as_of,
                 region_ids: list[str] | None = None, gpu_model_ids: list[str] | None = None) -> list[domain.PressureCell]:
    """Every (region, model) cell; cells with no asks are included with coverage 0 so a map can show the gap."""
    cells = []
    for rid in (region_ids or sorted(regions)):
        for gid in (gpu_model_ids or list(gpu_models)):
            cells.append(compute_cell(rid, gid, offers=offers, gpu_models=gpu_models, regions=regions, series=series,
                                      facilities=facilities, markets=markets, feed=feed, as_of=as_of))
    return cells


# -- history --------------------------------------------------------------------------------

def cell_history(
    region_id: str,
    gpu_model_id: str,
    snapshots: list[tuple[datetime, list[domain.GpuOffer]]],
    *,
    gpu_models, regions, series, facilities, markets, feed,
) -> tuple[list[domain.HistoryPoint], str]:
    """The score at every snapshot date from the data known then, plus the basis text.

    A snapshot is (date, the offers as they stood that day). Facilities are not
    dated in the data and are used as they are; everything else is cut at the date.
    """
    points: list[domain.HistoryPoint] = []
    sample_dates: list[datetime] = []
    observed_dates: list[datetime] = []
    for at, rows in sorted(snapshots, key=lambda s: s[0]):
        cell = compute_cell(region_id, gpu_model_id, offers=rows, gpu_models=gpu_models, regions=regions, series=series,
                            facilities=facilities, markets=markets, feed=feed, as_of=at)
        kinds = sorted({effective_kind(o.provenance) for o in rows})
        (sample_dates if set(kinds) <= {"sample"} else observed_dates).append(at)
        points.append(domain.HistoryPoint(t=at, score=cell.score, band=cell.band, coverage=cell.uncertainty.coverage,
                                          factors_with_data=cell.uncertainty.factors_with_data, label=cell.uncertainty.label,
                                          input_kinds=kinds))
    if observed_dates and sample_dates:
        basis = f"{len(sample_dates)} sample snapshots + FLOPX-collected since {min(observed_dates).date().isoformat()}"
    elif observed_dates:
        basis = f"FLOPX-collected since {min(observed_dates).date().isoformat()} ({len(observed_dates)} days); nothing earlier exists"
    elif sample_dates:
        basis = f"{len(sample_dates)} sample snapshots; FLOPX-collected history starts at first deploy"
    else:
        basis = "no history"
    return points, basis


def with_history(cell: domain.PressureCell, history: list[domain.HistoryPoint], basis: str) -> domain.PressureCell:
    return domain.PressureCell(**{**cell.__dict__, "history": history, "history_basis": basis})
