"""Scenario lab: elasticity projections over named assumptions, labelled demonstrative.

A scenario is not a forecast. It takes the drift baseline as the "nothing
changes" path and applies stated elasticities to the user's assumptions
(percent changes in supply, demand, energy price, equipment cost, regulation,
earnings), ramping the combined effect linearly over the horizon. The
elasticities are authored illustrative numbers, reported back with the path,
and every response carries `label: "demonstrative"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from product.flopx.forecast import baseline

# Elasticity = % change in target per 1% change in the assumption.
GPU_PRICE_ELASTICITIES = {
    "supplyGrowthPct": -0.60,
    "demandGrowthPct": 0.80,
    "energyPriceChangePct": 0.15,
    "equipmentCostChangePct": 0.25,
    "regulationTightnessPct": 0.10,
}
EQUITY_ELASTICITIES = {
    "supplyGrowthPct": -0.20,
    "demandGrowthPct": 0.50,
    "energyPriceChangePct": -0.10,
    "equipmentCostChangePct": -0.15,
    "regulationTightnessPct": -0.20,
    "earningsSurprisePct": 0.60,
}
# Per-company demand sensitivity (V4 F5: one elasticity for every company moved them all by the same
# percent). AUTHORED, never estimated: the percent equity move per percent compute-demand move, read off the
# company's most compute-exposed profile role, scaled down where the profile says part of the business is
# elsewhere. Whether any of it is a measured relationship is the evaluation harness's question.
ROLE_SENSITIVITY = {
    "gpu-cloud": 0.80, "neocloud": 0.80,
    "data-centre-developer": 0.55, "hosting": 0.55,
    "accelerator-vendor": 0.35,
    "picks-and-shovels": 0.30,
}
DIVERSIFIED_TAGS = {"miner-pivot": 0.75}


def company_sensitivity(company) -> tuple[float, str]:
    """(sensitivity, basis) for one company from its authored profile tags; the flat demand elasticity when no
    role tag applies. Roles are read in a fixed order so the basis names one tag."""
    tags = list(company.compute_exposure.tags)
    roles = [t for t in ROLE_SENSITIVITY if t in tags]
    if not roles:
        value = EQUITY_ELASTICITIES["demandGrowthPct"]
        return value, (f"authored flat demand elasticity {value:.2f} (no compute role in the profile); percent equity move per "
                       "percent compute-demand move, an estimate, not a measured beta")
    role = max(roles, key=lambda t: ROLE_SENSITIVITY[t])
    value = ROLE_SENSITIVITY[role]
    basis = f"authored sensitivity {value:.2f} for the profile role {role}"
    for tag, scale in DIVERSIFIED_TAGS.items():
        if tag in tags:
            value *= scale
            basis += f", x{scale:.2f} because the profile says {tag} (part of the business is not compute)"
    return round(value, 4), basis + "; percent equity move per percent compute-demand move, an estimate, not a measured beta"


ENERGY_ELASTICITIES = {
    "demandGrowthPct": 0.30,
    "regulationTightnessPct": 0.20,
}
# Uncertainty the assumptions themselves add on top of the baseline interval,
# as a fraction of the absolute scenario effect:
ASSUMPTION_UNCERTAINTY = 0.5


@dataclass(frozen=True)
class ScenarioPoint:
    step: int
    t: datetime
    baseline: float
    projected: float
    lower: float
    upper: float


@dataclass(frozen=True)
class Scenario:
    target: str
    label: str
    assumptions: dict[str, float]
    elasticities_used: dict[str, float]
    ignored_assumptions: list[str]
    total_effect_pct: float
    path: list[ScenarioPoint]
    caveats: list[str] = field(default_factory=list)


def elasticities_for(series_id: str) -> dict[str, float]:
    if series_id.startswith("equity:"):
        return EQUITY_ELASTICITIES
    if series_id.startswith("energy:"):
        return ENERGY_ELASTICITIES
    return GPU_PRICE_ELASTICITIES


def project(
    series_id: str,
    history: list[tuple[datetime, float]],
    assumptions: dict[str, float],
    horizon: int,
    *,
    step: timedelta = timedelta(days=1),
) -> Scenario:
    if len(history) < 3:
        raise ValueError("scenario needs at least three history points")
    y = [v for _, v in history]
    last_t = history[-1][0]
    table = elasticities_for(series_id)
    used = {k: table[k] for k in assumptions if k in table}
    ignored = sorted(k for k in assumptions if k not in table)
    total_effect = sum(used[k] * assumptions[k] for k in used) / 100.0

    base = baseline("drift", y, horizon, 7, 0.8)
    path: list[ScenarioPoint] = []
    for k in range(1, horizon + 1):
        ramp = k / horizon
        b = base.mean[k - 1]
        projected = b * (1.0 + total_effect * ramp)
        effect_abs = abs(projected - b)
        half = (base.upper[k - 1] - base.lower[k - 1]) / 2.0 + ASSUMPTION_UNCERTAINTY * effect_abs
        path.append(ScenarioPoint(step=k, t=last_t + step * k, baseline=b, projected=projected,
                                  lower=projected - half, upper=projected + half))

    caveats = ["demonstrative", "elasticities-authored-not-estimated", "linear-ramp"]
    if ignored:
        caveats.append("some-assumptions-ignored")
    return Scenario(
        target=series_id, label="demonstrative", assumptions=dict(assumptions),
        elasticities_used=used, ignored_assumptions=ignored,
        total_effect_pct=round(total_effect * 100.0, 3), path=path, caveats=caveats,
    )


# ============================================================================
# Infrastructure scenarios (architecture §10.4): forecast, conditional
# ============================================================================

INFRA_KINDS = ("delay-facility", "complete-facility", "energy-shock", "demand-shock", "utilisation")
MAX_INFRA_HORIZON_DAYS = 365
# Price responds to pressure with this elasticity: a full 50-point pressure move is a 25% comparable-price move.
PRESSURE_PRICE_ELASTICITY = 0.25
PRESSURE_SCALE_POINTS = 50.0
# How a change in effective supply reaches the pressure score, in points per 100% change (authored; the
# index has no supply factor of its own, so a scenario has to say how supply enters it):
SUPPLY_PRESSURE_POINTS = 50.0
# The projected series is a GPU price (USD per GPU-hour), which cannot go below zero. The drift band widens
# with the horizon and on a short or volatile history its lower edge crosses zero (R1-3: -13.21 around a
# 2.86 centre at 90 days); every path value is floored here and the floor is stated as a caveat.
PRICE_FLOOR = 0.0
PRICE_FLOOR_CAVEAT = "price-band-floored-at-zero"


@dataclass(frozen=True)
class InfraPoint:
    step: int
    t: datetime
    baseline: float
    baseline_lower: float
    baseline_upper: float
    conditional: float
    lower: float
    upper: float


@dataclass(frozen=True)
class ExposedMove:
    ticker: str
    documented: bool
    basis: list[str]
    sensitivity: float
    sensitivity_basis: str
    estimated_move_pct: float


@dataclass(frozen=True)
class InfraScenario:
    region_id: str
    gpu_model_id: str
    horizon_days: int
    assumptions: list[dict]
    applied: list[str]
    ignored: list[dict]
    baseline_supply: object            # domain.EffectiveSupply
    scenario_supply: object
    baseline_score: float
    scenario_score: float
    pressure_delta: float
    factor_deltas: dict[str, float]
    price_effect_pct: float
    price_series_id: str
    path: list[InfraPoint]
    exposure: list[ExposedMove]
    elasticities_used: dict[str, float]
    caveats: list[str]
    statement_class: str = "forecast"
    conditional: bool = True
    label: str = "demonstrative"


def project_infra(
    region_id: str,
    gpu_model_id: str,
    horizon_days: int,
    assumptions: list[dict],
    *,
    offers,
    gpu_models,
    regions,
    series,
    facilities,
    markets,
    feed,
    companies,
    as_of: datetime,
    filings=(),
) -> InfraScenario:
    """Recomputes effective supply and the pressure score under the assumptions, then projects the
    model's comparable price on top of the drift baseline and traces the move into exposed companies.
    Imported lazily: pressure and effective_supply import nothing from here, but keeping the dependency
    one-directional at module level keeps the plain `project()` free of the inferred-state layer."""
    from product.flopx import effective_supply as supply_engine, exposure as exposure_engine, pressure as pressure_engine
    from product.flopx.series import point_in_time

    if not 1 <= horizon_days <= MAX_INFRA_HORIZON_DAYS:
        raise ValueError(f"horizonDays must be between 1 and {MAX_INFRA_HORIZON_DAYS}")
    by_id = {f.id: f for f in facilities}
    utilisation = supply_engine.DEFAULT_UTILISATION
    overrides: dict[str, tuple[float, str]] = {}
    energy_pct = 0.0
    demand_shift = 0.0
    applied: list[str] = []
    ignored: list[dict] = []
    for a in assumptions:
        kind = a.get("kind")
        try:
            if kind == "utilisation":
                utilisation = float(a["value"])
                if not supply_engine.UTILISATION_RANGE[0] <= utilisation <= supply_engine.UTILISATION_RANGE[1]:
                    raise ValueError(f"utilisation outside {supply_engine.UTILISATION_RANGE}")
                applied.append(f"utilisation={utilisation}")
            elif kind == "energy-shock":
                energy_pct = float(a["pct"])
                applied.append(f"energy-shock={energy_pct:+}%")
            elif kind == "demand-shock":
                demand_shift = float(a["pct"]) / 100.0
                applied.append(f"demand-shock={float(a['pct']):+}%")
            elif kind in ("delay-facility", "complete-facility"):
                f = by_id.get(a.get("facilityId"))
                if f is None or f.region_id != region_id:
                    raise ValueError("facility not in the region")
                if kind == "delay-facility":
                    # A delay is lateness against the plan: the facility's months late rise by `months`, so its
                    # completion probability follows the same clamp(1 - late / 24) x 0.8 rule the baseline uses.
                    months = int(a["months"])
                    if months < 0:
                        raise ValueError("months must be non-negative")
                    late = supply_engine.months_late(f.timeline.expected_online, as_of)
                    if late is None:
                        p = max(0.0, supply_engine.NO_DATE_PROBABILITY * (1.0 - months / supply_engine.LATE_HORIZON_MONTHS))
                    else:
                        p = round(max(0.0, min(1.0, 1.0 - (late + months) / supply_engine.LATE_HORIZON_MONTHS)) * supply_engine.COMPLETION_CEILING, 4)
                    overrides[f.id] = (p, f"delayed {months} months")
                    applied.append(f"delay-facility:{f.id}+{months}m")
                else:
                    at_date = str(a["atDate"])
                    year, month, day = (int(p) for p in at_date.split("-")[:3])
                    completes_in_horizon = datetime(year, month, day, tzinfo=as_of.tzinfo) <= as_of + timedelta(days=horizon_days)
                    # A facility completed within the horizon counts at its full stated capacity (probability 1.0):
                    # the baseline's completion probability is an expectation of arrival, and arrival is now
                    # certain. The utilisation assumption is applied to the operating stock, not to capacity
                    # in its first period, so completing a facility never reads as less supply than expecting it.
                    overrides[f.id] = (1.0 if completes_in_horizon else supply_engine.completion_probability(at_date, as_of)[0],
                                       f"completed {at_date}" if completes_in_horizon else f"completion moved to {at_date}, beyond the horizon")
                    applied.append(f"complete-facility:{f.id}@{at_date}")
            else:
                raise ValueError(f"unknown assumption kind {kind!r}")
        except (KeyError, ValueError, TypeError) as exc:
            ignored.append({**a, "reason": str(exc) or "malformed"})

    base_supply = supply_engine.effective_supply(region_id, facilities, as_of)
    scen_supply = supply_engine.effective_supply(region_id, facilities, as_of, utilisation=utilisation, overrides=overrides)
    supply_change = (scen_supply.effective_mw / base_supply.effective_mw - 1.0) if base_supply.effective_mw > 0 else 0.0

    cell_kwargs = dict(offers=offers, gpu_models=gpu_models, regions=regions, facilities=facilities, markets=markets, feed=feed, as_of=as_of)
    base_cell = pressure_engine.compute_cell(region_id, gpu_model_id, series=series, **cell_kwargs)
    scen_series = dict(series)
    zone = regions[region_id].grid_zone if region_id in regions else None
    energy_id = f"energy:{zone}:day-ahead"
    if energy_pct and energy_id in series and series[energy_id].points:
        s = series[energy_id]
        last = s.points[-1]
        scen_series[energy_id] = type(s)(id=s.id, unit=s.unit, points=[*s.points[:-1], type(last)(series_id=last.series_id, t=last.t, value=last.value * (1 + energy_pct / 100.0), unit=last.unit, provenance=last.provenance)])
    scen_cell = pressure_engine.compute_cell(region_id, gpu_model_id, series=scen_series, **cell_kwargs)
    factors = []
    for f in scen_cell.factors:
        if f.factor == "demand-signals" and demand_shift:
            shifted = pressure_engine.clamp((f.normalised or 0.0) + demand_shift)
            f = type(f)(**{**f.__dict__, "normalised": round(shifted, 4), "contribution": round(f.weight * shifted, 4), "direction": pressure_engine._direction(shifted)})
        factors.append(f)
    scen_score = (pressure_engine.score_of(factors) or 50.0) - supply_change * SUPPLY_PRESSURE_POINTS
    scen_score = round(max(0.0, min(100.0, scen_score)), 2)
    deltas = {f.factor: round((f.contribution or 0.0) - (b.contribution or 0.0), 4) for f, b in zip(factors, base_cell.factors)}
    deltas["effective-supply"] = round(-supply_change * SUPPLY_PRESSURE_POINTS / 50.0, 4)
    pressure_delta = round(scen_score - base_cell.score, 2)
    effect = pressure_delta / PRESSURE_SCALE_POINTS * PRESSURE_PRICE_ELASTICITY

    # The projection steps by the day from daily points; GetDeploying's weekly median (the momentum factor's N-GDH-BE
    # fallback) is not a daily series, so the price path keeps the chain as it was: regional, index, global sample.
    price_series, _ = pressure_engine.momentum_series_for(series, gpu_model_id, region_id, weekly_fallback=False)
    if price_series is None:
        raise ValueError("no price series for the model")
    points = point_in_time(price_series, as_of)
    if len(points) < 3:
        raise ValueError("scenario needs at least three history points")
    y = [p.value for p in points]
    base = baseline("drift", y, horizon_days, 7, 0.8)
    path: list[InfraPoint] = []
    floored = False

    def floor(value: float) -> float:
        nonlocal floored
        if value < PRICE_FLOOR:
            floored = True
        return max(PRICE_FLOOR, round(value, 4))

    for k in range(1, horizon_days + 1):
        ramp = k / horizon_days
        b = base.mean[k - 1]
        projected = b * (1.0 + effect * ramp)
        half = (base.upper[k - 1] - base.lower[k - 1]) / 2.0 + ASSUMPTION_UNCERTAINTY * abs(projected - b)
        path.append(InfraPoint(step=k, t=points[-1].t + timedelta(days=k), baseline=floor(b), baseline_lower=floor(base.lower[k - 1]),
                               baseline_upper=floor(base.upper[k - 1]), conditional=floor(projected), lower=floor(projected - half), upper=floor(projected + half)))

    exposures = exposure_engine.trace(region_id, gpu_model_id, companies=companies, facilities=facilities, regions=regions, filings=filings)
    moves = [ExposedMove(ticker=e.ticker, documented=e.documented, basis=e.basis, sensitivity=e.sensitivity, sensitivity_basis=e.sensitivity_basis,
                         estimated_move_pct=round(effect * 100.0 * e.sensitivity, 3)) for e in exposures]
    caveats = ["demonstrative", "conditional-on-stated-assumptions", "elasticities-authored-not-estimated", "linear-ramp",
               "supply-to-pressure-link-is-an-assumption", "baseline-is-drift-not-a-forecast-of-record",
               "completed-facility-counts-at-stated-capacity"]
    if ignored:
        caveats.append("some-assumptions-ignored")
    if "sample" in base_cell.input_kinds:
        caveats.append("sample-data")
    if floored:
        caveats.append(PRICE_FLOOR_CAVEAT)
    return InfraScenario(
        region_id=region_id, gpu_model_id=gpu_model_id, horizon_days=horizon_days, assumptions=list(assumptions), applied=applied, ignored=ignored,
        baseline_supply=base_supply, scenario_supply=scen_supply, baseline_score=base_cell.score, scenario_score=scen_score, pressure_delta=pressure_delta,
        factor_deltas=deltas, price_effect_pct=round(effect * 100.0, 3), price_series_id=price_series.id, path=path, exposure=moves,
        elasticities_used={"pressureToPrice": PRESSURE_PRICE_ELASTICITY, "supplyToPressurePoints": SUPPLY_PRESSURE_POINTS,
                           "equityToPrice": EQUITY_ELASTICITIES["demandGrowthPct"], "assumptionUncertainty": ASSUMPTION_UNCERTAINTY},
        caveats=caveats,
    )
