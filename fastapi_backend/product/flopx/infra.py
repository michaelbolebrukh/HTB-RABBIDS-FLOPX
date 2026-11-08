"""Energy and infrastructure reads (architecture §10.9): observations, listed as they are.

Energy: per grid zone with a day-ahead series, the latest value, the trailing
30-day median and the change. Projects: every facility with its stage exactly
as the record says, stated capacity, timeline, months late, sources and the
owner links the data documents. Grid: regions with their zone, the latest
price and a constraint flag that is the pressure engine's power-stress factor
at or above 0.5. A facility is never shown as operating unless its status says
so; announced megawatts are never summed into operating (that sum lives in
effective_supply.py and is labelled an inference there).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from product.flopx import domain
from product.flopx.effective_supply import months_late
from product.flopx.pressure import POWER_WINDOW_DAYS, power_stress
from product.flopx.provenance import Provenance
from product.flopx.series import point_in_time

CONSTRAINT_THRESHOLD = 0.5
POINTS_SHOWN = 30


@dataclass(frozen=True)
class EnergyZone:
    zone: str
    series_id: str
    unit: str
    latest: float
    latest_at: datetime
    median_30d: float
    change_pct_30d: float | None
    points: list[domain.PriceObservation]
    regions: list[str]
    provenance: Provenance
    statement_class: str = "observation"


@dataclass(frozen=True)
class Project:
    facility: domain.Facility
    stage: str
    months_late: float | None
    documented_owners: list[dict]
    source_count: int
    statement_class: str = "observation"


@dataclass(frozen=True)
class GridRegion:
    region: domain.Region
    series_id: str | None
    latest_price: float | None
    unit: str | None
    power_stress_raw: float | None
    power_stress_normalised: float | None
    constrained: bool
    reason: str
    provenance: Provenance | None
    statement_class: str = "observation"
    flag_class: str = "inference"


def energy(regions: dict[str, domain.Region], series: dict[str, domain.Series], as_of: datetime) -> tuple[list[EnergyZone], list[dict]]:
    """Zones with a series, and the zones that have none (so a map can say so)."""
    zones: dict[str, list[str]] = {}
    for rid, r in sorted(regions.items()):
        if r.grid_zone:
            zones.setdefault(r.grid_zone, []).append(rid)
    rows, missing = [], []
    for zone, rids in sorted(zones.items()):
        sid = f"energy:{zone}:day-ahead"
        s = series.get(sid)
        points = point_in_time(s, as_of) if s else []
        if not points:
            missing.append({"zone": zone, "regions": rids, "reason": "no-series"})
            continue
        last = points[-1]
        window = [p for p in points if p.t >= last.t - timedelta(days=POWER_WINDOW_DAYS)]
        then = [p for p in points if p.t <= last.t - timedelta(days=30)]
        change = (last.value / then[-1].value - 1.0) * 100.0 if then and then[-1].value else None
        rows.append(EnergyZone(zone=zone, series_id=sid, unit=s.unit, latest=round(last.value, 4), latest_at=last.t,
                               median_30d=round(statistics.median(p.value for p in window), 4), change_pct_30d=round(change, 4) if change is not None else None,
                               points=points[-POINTS_SHOWN:], regions=rids, provenance=last.provenance))
    return rows, missing


def projects(facilities: list[domain.Facility], companies: dict[str, domain.Company], as_of: datetime,
             *, status: str | None = None, region: str | None = None) -> list[Project]:
    rows = []
    for f in facilities:
        if status and f.status not in status.split(","):
            continue
        if region and f.region_id not in region.split(","):
            continue
        owners = [{"ticker": t, "name": companies[t].name if t in companies else t, "basis": "facility-owner", "documented": True} for t in f.owner_company_ids]
        rows.append(Project(facility=f, stage=f.status, months_late=months_late(f.timeline.expected_online, as_of) if f.status != "operating" else None,
                            documented_owners=owners, source_count=len(f.sources)))
    order = {"operating": 0, "under-construction": 1, "announced": 2, "planned": 3, "reported": 4}
    rows.sort(key=lambda p: (order.get(p.stage, 9), -(p.facility.capacity_mw or 0), p.facility.id))
    return rows


def grid(regions: dict[str, domain.Region], series: dict[str, domain.Series], as_of: datetime) -> list[GridRegion]:
    rows = []
    for rid, r in sorted(regions.items()):
        factor = power_stress(series, r, as_of)
        sid = f"energy:{r.grid_zone}:day-ahead" if r.grid_zone else None
        last = factor.evidence[-1] if factor.evidence else None
        latest = None
        unit = None
        s = series.get(sid) if sid else None
        if s is not None:
            known = point_in_time(s, as_of)
            if known:
                latest, unit = known[-1].value, s.unit
        constrained = factor.normalised is not None and factor.normalised >= CONSTRAINT_THRESHOLD
        rows.append(GridRegion(region=r, series_id=sid if s is not None else None, latest_price=latest, unit=unit, power_stress_raw=factor.raw,
                               power_stress_normalised=factor.normalised, constrained=constrained,
                               reason=factor.note if factor.normalised is None else f"power-stress normalised {factor.normalised:+.2f} against threshold {CONSTRAINT_THRESHOLD}",
                               provenance=last))
    return rows
