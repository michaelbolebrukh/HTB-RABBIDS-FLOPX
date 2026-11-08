"""Power prices: Energy-Charts (DE-LU day-ahead) and Elexon BMRS (GB market index).

Energy-Charts (Fraunhofer ISE) returns `unix_seconds[]` and `price[]` in
EUR/MWh with an in-body `license_info` (CC BY 4.0, Bundesnetzagentur | SMARD.de);
that attribution string is carried in every point's provenance note, as the
licence requires. Only slots that have started by the fetch are kept: a
day-ahead price for a later slot is a schedule, not a current observation, so
the last point of the series is always the price in force now (V6 #5, #6).
Elexon returns half-hourly APX market index prices in
GBP/MWh with ISO `startTime`. Both carry their own timestamps, which are the
observation times. Series ids: `energy:DE-LU:day-ahead`, `energy:GB:market-index`.
The US grid zones (ERCOT for us-tx, PJM for us-va) have no keyless price source; what
the EIA key unlocks for them is hourly demand, in `eia.py`, not a price.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http

logger = logging.getLogger(__name__)

ENERGY_CHARTS = "energy-charts"
ELEXON = "elexon-bmrs"
ENERGY_CHARTS_ENDPOINT = "https://api.energy-charts.info/price"
ELEXON_ENDPOINT = "https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index"
ENERGY_CHARTS_LICENCE = licences.licence(ENERGY_CHARTS)
ELEXON_LICENCE = licences.licence(ELEXON)
ELEXON_ATTRIBUTION = licences.attribution(ELEXON)
CACHE_TTL_SECONDS = 900
ATTRIBUTION_FALLBACK = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"


def _window(days: int = 1) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def parse_energy_charts(payload: dict, zone: str, fetched_at) -> list[domain.PriceObservation]:
    unit = str((payload or {}).get("unit") or "EUR / MWh").replace(" ", "")
    attribution = str((payload or {}).get("license_info") or ATTRIBUTION_FALLBACK)
    series_id = f"energy:{zone}:day-ahead"
    points = []
    for seconds, price in zip((payload or {}).get("unix_seconds") or [], (payload or {}).get("price") or []):
        t = _http.epoch(seconds)
        value = _http.number(price)
        if t is None or value is None:
            continue
        # A slot that has not started yet is a schedule for tomorrow's delivery, not the price
        # now: it is left out, so the series (and every "current" value read from its last
        # point) ends at the slot in force at the fetch. The slot is dated by its own start.
        if t > fetched_at:
            continue
        points.append(domain.PriceObservation(
            series_id=series_id, t=t, value=round(value, 4), unit=unit,
            provenance=live(ENERGY_CHARTS, t, fetched_at=fetched_at, licence=ENERGY_CHARTS_LICENCE,
                            source_url="https://www.energy-charts.info/charts/price_spot_market/chart.htm?l=en&c=DE",
                            note=f"day-ahead price {zone} for the delivery slot starting at t; {attribution}; via Energy-Charts (Fraunhofer ISE)"),
        ))
    return points


def fetch_energy_charts(zone: str = "DE-LU", *, days: int = 1) -> SourceResult:
    start, end = _window(days)
    fetched = _http.fetch_json(ENERGY_CHARTS, ENERGY_CHARTS_ENDPOINT, key=f"flopx:energy-charts:{zone}:{start}:{end}", ttl_seconds=CACHE_TTL_SECONDS,
                               params={"bzn": zone, "start": start, "end": end})
    if isinstance(fetched, _http.Failure):
        return unavailable(ENERGY_CHARTS, fetched.reason)
    points = parse_energy_charts(fetched.payload, zone, fetched.fetched_at)
    if not points:
        return unavailable(ENERGY_CHARTS, "empty-response")
    attribution = str((fetched.payload or {}).get("license_info") or ATTRIBUTION_FALLBACK)
    newest = max(p.provenance.observed_at for p in points)
    return available(ENERGY_CHARTS, points, live(ENERGY_CHARTS, newest, fetched_at=fetched.fetched_at, licence=ENERGY_CHARTS_LICENCE, note=attribution))


def parse_elexon(payload: dict, fetched_at) -> list[domain.PriceObservation]:
    points = []
    for row in (payload or {}).get("data") or []:
        t = _http.iso(row.get("startTime"))
        value = _http.number(row.get("price"))
        if t is None or value is None or t > fetched_at:
            continue
        points.append(domain.PriceObservation(
            series_id="energy:GB:market-index", t=t, value=round(value, 4), unit="GBP/MWh",
            provenance=live(ELEXON, t, fetched_at=fetched_at, licence=ELEXON_LICENCE, source_url="https://bmrs.elexon.co.uk/market-index-price",
                            note=f"half-hourly market index price, provider {row.get('dataProvider')}, settlement period {row.get('settlementPeriod')}; {ELEXON_ATTRIBUTION}"),
        ))
    points.sort(key=lambda p: p.t)
    return points


def fetch_elexon(*, days: int = 1) -> SourceResult:
    start, end = _window(days)
    fetched = _http.fetch_json(ELEXON, ELEXON_ENDPOINT, key=f"flopx:elexon:mid:{start}:{end}", ttl_seconds=CACHE_TTL_SECONDS,
                               params={"from": f"{start}T00:00Z", "to": f"{end}T23:59Z", "dataProviders": "APXMIDP"})
    if isinstance(fetched, _http.Failure):
        return unavailable(ELEXON, fetched.reason)
    points = parse_elexon(fetched.payload, fetched.fetched_at)
    if not points:
        return unavailable(ELEXON, "empty-response")
    return available(ELEXON, points, live(ELEXON, points[-1].t, fetched_at=fetched.fetched_at, licence=ELEXON_LICENCE, note=ELEXON_ATTRIBUTION))
