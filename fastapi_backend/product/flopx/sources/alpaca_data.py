"""Alpaca Market Data: daily equity bars from the founder's own account, for the research archive.

Why this exists: the founder's CRSP extract ends on 2025-12-31 (decision G13) and Yahoo's
unofficial endpoint is permanently rate-limited on Render, so 2026 closes come from
`GET https://data.alpaca.markets/v2/stocks/bars` (timeframe 1Day, `adjustment=split`, the
`iex` feed of the free Basic plan), authenticated with the `PRODUCT_ALPACA_API_KEY` /
`PRODUCT_ALPACA_SECRET_KEY` the backend already holds. The keys travel only as request
headers; `_http` logs the host and path of a request and never its headers, and this module
never formats them into anything.

Licence (row `alpaca-data` in sources/licences.json, use `research-archive`): Alpaca's Terms
and Conditions, read 2026-11-08, provide the Content "exclusively for personal and
noncommercial access and use" and forbid republishing or publicly displaying it "to any
other computer, server, web site or other medium for publication or distribution" without
written consent. So the bars are stored for the founder's own research and reach signed-in
accounts only, exactly like the WRDS rows; everything derived from them (evaluation,
backtests, signals) stays public. `display-and-archive` would have asserted a right to show
the raw bars to the world that the terms do not give.

Shape of a bar: Alpaca keys a 1Day bar at midnight New York time (`"t": "2026-01-02T05:00:00Z"`),
so the session date is that instant's New York calendar day, and the observation time is the
session close in UTC by the one rule FLOPX uses for daily closes, `yahoo.bar_close_times`
over the 09:30 New York open (21:00 UTC in winter, 20:00 UTC in summer), never the bar's key
and never the fetch time. `adjustment=split` returns closes divided by every later split, the
same convention as CRSP's `close / cfacpr`, so NVDA's 2024-06-06 bar reads 121.09 (not
1,210.9) on both sides of the join (recorded fixture `bars-nvda-smci-2024-splits.json`).

Fetching bypasses the refresher's serving snapshot on purpose: `fetch_bars` is a loader's
write path (the archive is the reader), so it goes through the platform cache and the
per-host Retry-After block of `_http` but never answers from a held snapshot. A 429 blocks
the host for its Retry-After, an auth failure comes back as `http-401`/`http-403`, and every
failure is an `unavailable` result, never an exception. One request per ticker per window:
`limit=10000` holds many years of daily bars, so a `next_page_token` is logged and left
alone (the loader would need several requests to follow it, and the brief bounds the boot
at eight).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from infrastructure.settings import settings as s
from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, _http, available, licences, unavailable, yahoo

logger = logging.getLogger(__name__)

SOURCE = "alpaca-data"
LICENCE = licences.licence(SOURCE)
ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
SOURCE_URL = "https://alpaca.markets/data"
TIMEFRAME = "1Day"
ADJUSTMENT = "split"
FEED = "iex"
PAGE_LIMIT = 10000
CACHE_TTL_SECONDS = 6 * 3600
NOTE = ("Alpaca Market Data daily bar from the founder's account (IEX feed, split-adjusted), stamped at the session close; "
        "personal and noncommercial use under Alpaca's terms, so the raw bar reaches signed-in accounts only")
EXCHANGE_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)


def configured() -> bool:
    """True when both market-data keys are present; the values are read for truthiness only."""
    return bool(s.PRODUCT_ALPACA_API_KEY and s.PRODUCT_ALPACA_SECRET_KEY)


def _headers() -> dict[str, str]:
    return {"APCA-API-KEY-ID": str(s.PRODUCT_ALPACA_API_KEY), "APCA-API-SECRET-KEY": str(s.PRODUCT_ALPACA_SECRET_KEY)}


def session_close_utc(day: date) -> datetime:
    """When that day's close was known: the Yahoo adapter's rule over the New York session open."""
    open_at = datetime(day.year, day.month, day.day, *SESSION_OPEN, tzinfo=EXCHANGE_TZ)
    close_ts = yahoo.bar_close_times([int(open_at.timestamp())], "1d", {})[0]
    return datetime.fromtimestamp(close_ts, tz=timezone.utc)


def session_day(bar_time: datetime) -> date:
    """The New York calendar day a 1Day bar's key falls on (Alpaca keys it at local midnight, written in UTC)."""
    return bar_time.astimezone(EXCHANGE_TZ).date()


def parse_bars(ticker: str, payload: dict, fetched_at: datetime) -> list[domain.EquityBar]:
    """The bars for `ticker` in a /v2/stocks/bars body, oldest first; a bar with no close or an unreadable time is skipped."""
    symbol = ticker.upper()
    rows = ((payload or {}).get("bars") or {}).get(symbol) or []
    bars: list[domain.EquityBar] = []
    for raw in rows:
        at = _http.iso(raw.get("t")) if isinstance(raw, dict) else None
        close = _http.number(raw.get("c")) if isinstance(raw, dict) else None
        if at is None or close is None:
            continue
        t = session_close_utc(session_day(at))
        bars.append(domain.EquityBar(
            ticker=symbol, t=t,
            open=_http.number(raw.get("o")) or close, high=_http.number(raw.get("h")) or close,
            low=_http.number(raw.get("l")) or close, close=close, volume=_http.number(raw.get("v")) or 0.0,
            provenance=live(SOURCE, t, fetched_at=fetched_at, licence=LICENCE, source_url=SOURCE_URL, note=NOTE),
        ))
    bars.sort(key=lambda b: b.t)
    return bars


def fetch_bars(ticker: str, start: date, end: date | None = None) -> SourceResult:
    """Daily bars for `ticker` from `start` through `end` (today when None); one request, cached six hours; never raises."""
    if not configured():
        return unavailable(SOURCE, "not-configured")
    symbol = ticker.upper()
    today = datetime.now(timezone.utc).date()
    params = {"symbols": symbol, "timeframe": TIMEFRAME, "start": start.isoformat(), "adjustment": ADJUSTMENT, "feed": FEED,
              "limit": PAGE_LIMIT, "sort": "asc"}
    if end is not None:
        params["end"] = end.isoformat()
    # The key carries today's date so an open-ended window is asked again on a new day, and never a credential.
    key = f"flopx:alpaca-data:bars:{symbol}:{start.isoformat()}:{end.isoformat() if end else 'open'}:{today.isoformat()}"
    # Not `_http.fetch_json`: in serving mode that answers from the refresher's snapshot and makes no request,
    # and a loader must fetch. The cache and the host's Retry-After block still apply.
    fetched = _http._through_cache(key, CACHE_TTL_SECONDS, lambda: _http._request(
        SOURCE, "GET", ENDPOINT, params=params, headers=_headers(), json=None, as_json=True))
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    if (fetched.payload or {}).get("next_page_token"):
        logger.warning("%s: %s %s..%s has more bars than one page; only the first %d were taken", SOURCE, symbol, start, end or "today", PAGE_LIMIT)
    bars = parse_bars(symbol, fetched.payload, fetched.fetched_at)
    if not bars:
        return unavailable(SOURCE, "empty-response")
    prov = live(SOURCE, bars[-1].t, fetched_at=fetched.fetched_at, licence=LICENCE, source_url=SOURCE_URL, note=NOTE)
    return available(SOURCE, bars, prov)
