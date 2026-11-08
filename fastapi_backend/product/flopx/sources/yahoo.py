"""Yahoo Finance chart adapter: equity bars, unofficial endpoint, display only.

Same endpoint the frontend route handler uses
(`https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=&range=`),
browser User-Agent, through the shared `_http` seam: 5 s timeout, a 300 s
TTL-only cache per request shape, at most one request per second to the host,
and a 429 that blocks the host for its `Retry-After` (ten minutes when Yahoo
gives none). The licence row is `display-only` (sources/licences.json): bars
are shown live and the archive refuses to store them.

Observation time: a bar is stamped when its value was known, never at its
start. Yahoo keys a daily bar at the session OPEN (14:30 UTC), so a daily bar is
stamped at the open plus the regular-session length (`currentTradingPeriod`,
6.5 h when absent: 21:00 UTC in winter, 20:00 UTC in summer); an intraday bar at
its start plus its interval; a weekly or monthly bar at the next bar's start.
The newest bar of a session still in progress is stamped at
`regularMarketTime`, the last trade it reflects. `bar_close_times` is the one
rule, mirrored by `lib/sources/parse/yahoo.ts` and pinned by both tiers' tests
over the same recorded body.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import _http, licences

logger = logging.getLogger(__name__)

SOURCE = "yahoo-finance"
LICENCE = licences.licence(SOURCE)
NOTE = "unofficial chart endpoint, delayed; display only, never archived; stamped at the session close (or the last trade for a session in progress)"
ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CACHE_TTL_SECONDS = 300
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
VALID_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"}
US_SESSION_SECONDS = 6 * 3600 + 30 * 60
PERIOD_SECONDS = {"1wk": 7 * 86400, "1mo": 31 * 86400}


def bar_close_times(timestamps: list[int], interval: str, meta: dict) -> list[int]:
    """Epoch seconds at which each bar's close was known (see the module docstring)."""
    regular = ((meta or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = regular.get("start"), regular.get("end")
    session = int(end) - int(start) if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start else US_SESSION_SECONDS
    market_time = (meta or {}).get("regularMarketTime")
    out: list[int] = []
    for i, ts in enumerate(timestamps):
        if interval == "1d":
            close = ts + session
        elif interval.endswith("m"):
            close = ts + int(interval[:-1]) * 60
        else:
            close = timestamps[i + 1] if i + 1 < len(timestamps) else ts + PERIOD_SECONDS.get(interval, 86400)
        if isinstance(market_time, (int, float)) and ts <= market_time < close:
            close = int(market_time)   # the session is still trading: the value is the last trade's
        out.append(int(close))
    return out


def _parse(symbol: str, payload: dict, fetched_at: datetime, interval: str = "1d") -> tuple[list[domain.EquityBar], datetime | None]:
    result = ((payload or {}).get("chart") or {}).get("result") or []
    if not result:
        return [], None
    chart = result[0]
    meta = chart.get("meta") or {}
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    market_time = meta.get("regularMarketTime")
    market_dt = datetime.fromtimestamp(market_time, tz=timezone.utc) if market_time else None
    closes_at = bar_close_times(timestamps, interval, meta)
    bars: list[domain.EquityBar] = []
    for i, ts in enumerate(timestamps):
        close = (quote.get("close") or [None])[i] if i < len(quote.get("close") or []) else None
        if close is None:
            continue
        t = datetime.fromtimestamp(closes_at[i], tz=timezone.utc)
        bars.append(domain.EquityBar(
            ticker=symbol, t=t,
            open=(quote.get("open") or [close])[i] or close,
            high=(quote.get("high") or [close])[i] or close,
            low=(quote.get("low") or [close])[i] or close,
            close=close,
            volume=(quote.get("volume") or [0])[i] or 0,
            provenance=live(SOURCE, t, fetched_at=fetched_at, licence=LICENCE,
                            source_url=f"https://finance.yahoo.com/quote/{symbol}", note=NOTE),
        ))
    return bars, market_dt


def fetch_bars(ticker: str, range_: str = "6mo", interval: str = "1d") -> SourceResult:
    """Bars for a ticker; cached per (ticker, range, interval) through `_http`; never raises."""
    if range_ not in VALID_RANGES or interval not in VALID_INTERVALS:
        return unavailable(SOURCE, "invalid-range-or-interval")
    symbol = ticker.upper()
    fetched = _http.fetch_json(SOURCE, ENDPOINT.format(symbol=symbol), key=f"flopx:yahoo:{symbol}:{range_}:{interval}", ttl_seconds=CACHE_TTL_SECONDS,
                               params={"interval": interval, "range": range_}, headers={"User-Agent": USER_AGENT})
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    bars, _ = _parse(symbol, fetched.payload, fetched.fetched_at, interval)
    if not bars:
        return unavailable(SOURCE, "empty-response")
    latest = bars[-1]
    prov = live(SOURCE, latest.t, fetched_at=fetched.fetched_at, licence=LICENCE,
                source_url=f"https://finance.yahoo.com/quote/{symbol}", note=NOTE)
    return available(SOURCE, bars, prov)


def fetch_latest_close(ticker: str) -> domain.PriceObservation | None:
    """The most recent daily close as a live observation, or None when unavailable."""
    result = fetch_bars(ticker, "5d", "1d")
    if not result.ok or not result.records:
        return None
    bar = result.records[-1]
    return domain.PriceObservation(
        series_id=f"equity:{ticker.upper()}:close", t=bar.t, value=bar.close, unit="USD",
        provenance=result.provenance or bar.provenance,
    )
