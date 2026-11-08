"""EIA API v2, Form EIA-930: hourly demand and the day-ahead demand forecast for ERCOT and PJM.

Why demand and not a price. The brief asked for the ERCOT and PJM day-ahead wholesale price
(USD/MWh) from `electricity/rto/...`, falling back to the real-time LMP. Neither exists in
EIA API v2 (read 2026-11-08 by listing every route under `/v2/electricity/`): the `rto`
routes are Form EIA-930, the Hourly Electric Grid Monitor, and carry demand, the day-ahead
demand forecast, net generation and interchange in megawatthours, hourly and daily, for
each balancing authority; the only price anywhere under `/v2/electricity/` is the monthly
retail average in `retail-sales` (cents per kWh by state, about two months behind). EIA's
wholesale day-ahead prices (ERCOT North, PJM West) are a biweekly spreadsheet republished
from ICE with permission, third-party copyright, outside the API and outside the public
domain, so they are not touched. What the key unlocks is therefore the grid's load side:
`energy:ERCOT:demand`, `energy:ERCOT:demand-forecast`, `energy:PJM:demand` and
`energy:PJM:demand-forecast`, hourly, in MWh, with the hour's start as the observation time.
They are live, archivable, public-domain series; they are NOT the `energy:<zone>:day-ahead`
price the power-stress factor and the infra energy read consume, and they are never written
under that id, so `us-tx` and `us-va` keep their sample price input until a licensed price
source is found. A forecast slot for an hour that has not started is a schedule, not an
observation, and is left out like Energy-Charts' future slots; the forecast kept for a past
hour is what was forecast for it, which is what pairs with the demand actually met.

The key. `EIA_API_KEY` (infrastructure/settings.py) travels only as the `api_key` query
parameter the API requires; `_http` logs a request's host and path and never its query
string, the cache key is built from the zone and window, and no `Fetched.url` (which would
carry the parameter) is ever copied into a provenance, a note or a log. Without the key the
adapter is dormant: `configured()` is False, `fetch_zone` answers `unavailable` with reason
`not-configured` without a request, and the terminal leaves these series out of every call
list, so the zones read exactly as before the adapter existed.

Rate limits. The API answers with `x-ratelimit-limit` headers (10 on the demo key, window
unstated) and 429 when exceeded; `api.eia.gov` is in `_http.MIN_INTERVAL_SECONDS`, so this
process never sends two requests to it inside a second, and a 429 blocks the host for its
Retry-After. One request covers both types for one zone (two rows per hour); a response holds
at most 5000 rows, so the history backfill asks one type per request in chunks of
`HISTORY_CHUNK_DAYS` days (200 x 24 = 4800 rows), well inside the cap.

Licence: row `eia` in sources/licences.json, "US public domain (EIA)": EIA's copyright page
(read 2026-11-08) says U.S. government publications are in the public domain and its data
may be used and distributed; the exception it names, third-party material such as the ICE
prices, is exactly what this adapter does not read.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from infrastructure.settings import settings as s
from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, _http, available, licences, unavailable

logger = logging.getLogger(__name__)

SOURCE = "eia"
LICENCE = licences.licence(SOURCE)
ATTRIBUTION = licences.attribution(SOURCE)
ENDPOINT = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
SOURCE_URL = "https://www.eia.gov/electricity/gridmonitor/"
UNIT = "MWh"
CACHE_TTL_SECONDS = 3600          # EIA-930 publishes each hour once; the forecast for a day lands the evening before
NOT_CONFIGURED = "not-configured"
PAGE_LIMIT = 5000                 # the API's hard cap on rows per response (measured: length=6000 answers 5000 and a warning)
HISTORY_CHUNK_DAYS = 200          # one type per request: 200 days x 24 hours = 4800 rows
PERIOD_FORMAT = "%Y-%m-%dT%H"

# FLOPX grid zone -> EIA-930 balancing-authority respondent id
ZONES = {"ERCOT": "ERCO", "PJM": "PJM"}
# EIA-930 type -> the series suffix under energy:<zone>:
TYPES = {"D": "demand", "DF": "demand-forecast"}
SERIES_IDS = tuple(f"energy:{zone}:{suffix}" for zone in ZONES for suffix in TYPES.values())


def configured() -> bool:
    """True when the key is set; read for truthiness only, never formatted into anything."""
    return bool(s.EIA_API_KEY)


def series_id(zone: str, type_code: str) -> str:
    return f"energy:{zone}:{TYPES[type_code]}"


def zone_of(sid: str) -> tuple[str, str]:
    """(zone, type code) for one of SERIES_IDS; KeyError otherwise."""
    _, zone, suffix = sid.split(":", 2)
    code = next(k for k, v in TYPES.items() if v == suffix)
    if zone not in ZONES:
        raise KeyError(sid)
    return zone, code


def period(text: str | None) -> datetime | None:
    """`2026-11-08T09` (the hourly frequency's UTC period) as an aware datetime at the hour's start."""
    if not text:
        return None
    try:
        return datetime.strptime(str(text).strip(), PERIOD_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _params(zone: str, first: date, last: date, types: tuple[str, ...]) -> dict:
    """The query for the inclusive UTC days [first, last]; the key is added at the one place that sends it."""
    out = {"frequency": "hourly", "data[0]": "value", "facets[respondent][0]": ZONES[zone],
           "start": f"{first.isoformat()}T00", "end": f"{last.isoformat()}T23",
           "sort[0][column]": "period", "sort[0][direction]": "asc", "length": str(PAGE_LIMIT)}
    for i, code in enumerate(types):
        out[f"facets[type][{i}]"] = code
    return out


def _keyed(params: dict) -> dict:
    return {"api_key": str(s.EIA_API_KEY), **params}


def _rows(payload) -> list:
    """The data rows alone (B-MEM: the held answer is the slice read, not the response envelope)."""
    return list(((payload or {}).get("response") or {}).get("data") or [])


def parse(payload, zone: str, fetched_at: datetime) -> list[domain.PriceObservation]:
    """Every row of a region-data response as a point of its zone's series, slots not yet started left out, oldest first.
    Accepts the reduced row list or the whole response envelope."""
    rows = payload if isinstance(payload, list) else _rows(payload)
    respondent = ZONES[zone]
    points = []
    for row in rows:
        if not isinstance(row, dict) or row.get("respondent") != respondent or row.get("type") not in TYPES:
            continue
        t = period(row.get("period"))
        value = _http.number(row.get("value"))
        if t is None or value is None or t > fetched_at:
            continue
        code = str(row["type"])
        name = str(row.get("type-name") or TYPES[code])
        points.append(domain.PriceObservation(
            series_id=series_id(zone, code), t=t, value=round(value, 4), unit=UNIT,
            provenance=live(SOURCE, t, fetched_at=fetched_at, licence=LICENCE, source_url=SOURCE_URL,
                            note=f"hourly {name.lower()} for {zone} ({row.get('respondent-name') or respondent}) for the hour starting at t, "
                                 f"Form EIA-930 via the EIA API v2; {ATTRIBUTION}"),
        ))
    points.sort(key=lambda p: (p.t, p.series_id))
    return points


def _window(days: int) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=days), today


def fetch_zone(zone: str, *, days: int = 1) -> SourceResult:
    """Yesterday's and today's demand and day-ahead demand forecast for one zone, both series in one result;
    `unavailable(not-configured)` without the key, and no request is made."""
    if zone not in ZONES:
        return unavailable(SOURCE, "unknown-zone")
    if not configured():
        return unavailable(SOURCE, NOT_CONFIGURED)
    first, last = _window(days)
    fetched = _http.fetch_json(SOURCE, ENDPOINT, key=f"flopx:eia:{zone}:{first}:{last}", ttl_seconds=CACHE_TTL_SECONDS,
                               params=_keyed(_params(zone, first, last, tuple(TYPES))), reduce=_rows)
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    points = parse(fetched.payload, zone, fetched.fetched_at)
    if not points:
        return unavailable(SOURCE, "empty-response")
    return available(SOURCE, points, live(SOURCE, points[-1].t, fetched_at=fetched.fetched_at, licence=LICENCE, note=ATTRIBUTION))


def history_params(sid: str, first: date, last: date) -> dict:
    """The history backfill's query for one series over the inclusive UTC days [first, last], key included."""
    zone, code = zone_of(sid)
    return _keyed(_params(zone, first, last, (code,)))


def parse_series(sid: str, payload, fetched_at: datetime) -> list[domain.PriceObservation]:
    """The points of one series only, for the history backfill (a response may carry both types)."""
    zone, _ = zone_of(sid)
    return [p for p in parse(payload, zone, fetched_at) if p.series_id == sid]
