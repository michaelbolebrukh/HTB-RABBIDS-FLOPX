"""Kalshi public market reads (research R3, section C).

`GET /trade-api/v2/markets?series_ticker=...&status=open` without auth, one
request per series the terminal follows (US data-centre capacity, US
electricity price, ERCOT peak load, PJM West utility price). Prices arrive as
dollar strings in [0, 1]; `updated_time` is the upstream's own observation
time. Candlesticks (one request per market, on demand) give the outcome
series. A series with no open market today (PJM West at the time of writing)
contributes nothing and is listed under `unavailable` with that reason.
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

SOURCE = "kalshi"
LICENCE = licences.licence(SOURCE)
BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_TTL_SECONDS = 300
HISTORY_TTL_SECONDS = 900
ID_PREFIX = "kalshi:"
NOTE = "a quote is a price in dollars per contract, never a calibrated probability"
# series ticker -> (regions it concerns, tickers it concerns)
SERIES = {
    "KXUSDCCAPACITY": ([], []),
    "KXPOWERKWH": ([], []),
    "KXTXERCOTPEAK": (["us-tx"], []),
    "KXUTILITYPJMWEST": (["us-va"], []),
}


def _market(raw: dict, series: str, fetched_at) -> domain.PredictionMarket | None:
    yes = _http.number(raw.get("last_price_dollars"))
    if yes is None:
        bid, ask = _http.number(raw.get("yes_bid_dollars")), _http.number(raw.get("yes_ask_dollars"))
        yes = (bid + ask) / 2 if bid is not None and ask is not None else None
    if yes is None:
        return None
    yes = round(min(max(yes, 0.0), 1.0), 4)
    updated = _http.iso(raw.get("updated_time")) or _http.iso(raw.get("open_time")) or fetched_at
    regions, tickers = SERIES.get(series, ([], []))
    ticker = str(raw.get("ticker"))
    return domain.PredictionMarket(
        id=f"{ID_PREFIX}{ticker}", venue="kalshi", question=str(raw.get("title") or ticker),
        outcomes=[domain.OutcomeQuote(label=f"Yes ({raw.get('yes_sub_title') or 'Yes'})", price=yes, implied_probability=yes),
                  domain.OutcomeQuote(label="No", price=round(1.0 - yes, 4), implied_probability=round(1.0 - yes, 4))],
        last_updated=updated, resolution_rules=raw.get("rules_primary"),
        liquidity=_http.number(raw.get("liquidity_dollars")) or _http.number(raw.get("open_interest_fp")),
        volume_24h=_http.number(raw.get("volume_24h_fp")),
        linked_entities=domain.LinkedEntities(tickers=list(tickers), region_ids=list(regions)),
        provenance=live(SOURCE, updated, fetched_at=fetched_at, licence=LICENCE,
                        source_url=f"https://kalshi.com/markets/{series.lower()}", note=f"{NOTE}; last trade {yes:.2f}, volume {raw.get('volume_fp')}"),
    )


def parse(payload: dict, series: str, fetched_at) -> list[domain.PredictionMarket]:
    markets = []
    for raw in (payload or {}).get("markets") or []:
        if raw.get("status") not in (None, "active", "open"):
            continue
        market = _market(raw, series, fetched_at)
        if market is not None:
            markets.append(market)
    return markets


def fetch_series(series: str) -> SourceResult:
    fetched = _http.fetch_json(SOURCE, f"{BASE}/markets", key=f"flopx:kalshi:markets:{series}", ttl_seconds=CACHE_TTL_SECONDS,
                               params={"series_ticker": series, "limit": 20, "status": "open"})
    if isinstance(fetched, _http.Failure):
        return unavailable(f"{SOURCE}:{series}", fetched.reason)
    markets = parse(fetched.payload, series, fetched.fetched_at)
    if not markets:
        return unavailable(f"{SOURCE}:{series}", "no-open-markets")
    newest = max(m.last_updated for m in markets)
    return available(SOURCE, markets, live(SOURCE, newest, fetched_at=fetched.fetched_at, licence=LICENCE, note=NOTE))


def fetch_prediction_markets() -> list[SourceResult]:
    """One result per followed series, so freshness can name each one."""
    return [fetch_series(series) for series in SERIES]


def parse_history(payload: dict, market_id: str, fetched_at) -> list[domain.PriceObservation]:
    points = []
    for candle in (payload or {}).get("candlesticks") or []:
        t = _http.epoch(candle.get("end_period_ts"))
        price = candle.get("price") or {}
        value = _http.number(price.get("close_dollars"))
        if value is None:
            value = _http.number(price.get("previous_dollars"))
        if value is None:
            bid = _http.number((candle.get("yes_bid") or {}).get("close_dollars"))
            ask = _http.number((candle.get("yes_ask") or {}).get("close_dollars"))
            value = (bid + ask) / 2 if bid is not None and ask is not None else None
        if t is None or value is None:
            continue
        points.append(domain.PriceObservation(
            series_id=f"market:{market_id}:yes", t=t, value=round(value, 4), unit="probability",
            provenance=live(SOURCE, t, fetched_at=fetched_at, licence=LICENCE, note="daily candle close of the Yes contract; a price, not a probability"),
        ))
    points.sort(key=lambda p: p.t)
    return points


def fetch_market_history(market_id: str, *, days: int = 180) -> SourceResult:
    """Daily candlesticks for one Kalshi market id (`kalshi:<ticker>`); one request."""
    ticker = market_id[len(ID_PREFIX):] if market_id.startswith(ID_PREFIX) else market_id
    series = ticker.split("-")[0]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    fetched = _http.fetch_json(SOURCE, f"{BASE}/series/{series}/markets/{ticker}/candlesticks", key=f"flopx:kalshi:candles:{ticker}",
                               ttl_seconds=HISTORY_TTL_SECONDS, params={"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": 1440})
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    points = parse_history(fetched.payload, market_id, fetched.fetched_at)
    if not points:
        return unavailable(SOURCE, "empty-history")
    return available(SOURCE, points, live(SOURCE, points[-1].t, fetched_at=fetched.fetched_at, licence=LICENCE))
