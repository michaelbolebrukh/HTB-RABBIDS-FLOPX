"""Polymarket GPU rental price markets (research R3, section C).

One Gamma `public-search` call for "GPU rental prices" returns the events whose
bracket markets settle on the Ornn GPU price index; every active market in
those events becomes a PredictionMarket with the real `updatedAt` as its
observation time. The CLOB `prices-history` endpoint supplies one outcome
series per market (one request per market, only when a market detail is
asked for, cached 15 minutes). A quote is a price, never a calibrated
probability, and every record says so.
"""

from __future__ import annotations

import json
import logging
import re

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http
from product.flopx.sources._mapping import gpu_models_in

logger = logging.getLogger(__name__)

SOURCE = "polymarket-gamma"
HISTORY_SOURCE = "polymarket-clob"
LICENCE = licences.licence(SOURCE)
SEARCH_ENDPOINT = "https://gamma-api.polymarket.com/public-search"
HISTORY_ENDPOINT = "https://clob.polymarket.com/prices-history"
SEARCH_QUERY = "GPU rental prices"
CACHE_TTL_SECONDS = 300
HISTORY_TTL_SECONDS = 900
ID_PREFIX = "polymarket:"
NOTE = "a quote is a price in USDC, never a calibrated probability; bracket markets settle on the Ornn GPU price index"


def _list(text) -> list:
    if isinstance(text, list):
        return text
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _rules(market: dict, event: dict) -> str:
    description = str(market.get("description") or event.get("description") or "").strip()
    first = description.split("\n\n")[0][:600]
    source = market.get("resolutionSource") or event.get("resolutionSource")
    return f"{first} Resolution source: {source}" if source else first


def _settles_on_ornn(market: dict, event: dict) -> bool:
    blob = " ".join(str(market.get(k) or event.get(k) or "") for k in ("resolutionSource", "description")).lower()
    return "ornn" in blob


def parse(payload: dict, fetched_at) -> tuple[list[domain.PredictionMarket], dict[str, str]]:
    """Markets plus the Yes-outcome CLOB token per market id (needed for history)."""
    markets: list[domain.PredictionMarket] = []
    tokens: dict[str, str] = {}
    for event in (payload or {}).get("events") or []:
        title = str(event.get("title") or "")
        if not re.match(r"^GPU rental prices", title, re.IGNORECASE) or not event.get("active") or event.get("closed"):
            continue
        for market in event.get("markets") or []:
            if market.get("closed") or not market.get("active") or not _settles_on_ornn(market, event):
                continue
            labels = _list(market.get("outcomes"))
            prices = [_http.number(p) for p in _list(market.get("outcomePrices"))]
            if len(labels) != len(prices) or not labels or any(p is None for p in prices):
                continue
            updated = _http.iso(market.get("updatedAt")) or _http.iso(event.get("updatedAt")) or fetched_at
            market_id = f"{ID_PREFIX}{market.get('id')}"
            clob = _list(market.get("clobTokenIds"))
            if clob:
                tokens[market_id] = str(clob[0])
            markets.append(domain.PredictionMarket(
                id=market_id, venue="polymarket", question=str(market.get("question") or f"{title} {market.get('groupItemTitle', '')}").strip(),
                outcomes=[domain.OutcomeQuote(label=str(l), price=round(min(max(p, 0.0), 1.0), 4), implied_probability=round(min(max(p, 0.0), 1.0), 4)) for l, p in zip(labels, prices)],
                last_updated=updated, resolution_rules=_rules(market, event),
                liquidity=_http.number(market.get("liquidityNum")), volume_24h=_http.number(market.get("volume24hr")),
                linked_entities=domain.LinkedEntities(),
                provenance=live(SOURCE, updated, fetched_at=fetched_at, licence=LICENCE,
                                source_url=f"https://polymarket.com/event/{event.get('slug')}", note=NOTE),
            ))
    return markets, tokens


def gpu_model_ids_for(market: domain.PredictionMarket) -> list[str]:
    return gpu_models_in(market.question)


def _search():
    return _http.fetch_json(SOURCE, SEARCH_ENDPOINT, key="flopx:polymarket:search", ttl_seconds=CACHE_TTL_SECONDS,
                            params={"q": SEARCH_QUERY, "limit_per_type": 12})


def fetch_prediction_markets() -> SourceResult:
    fetched = _search()
    if isinstance(fetched, _http.Failure):
        return unavailable(SOURCE, fetched.reason)
    markets, _ = parse(fetched.payload, fetched.fetched_at)
    if not markets:
        return unavailable(SOURCE, "no-matching-markets")
    markets.sort(key=lambda m: m.volume_24h or 0.0, reverse=True)
    newest = max(m.last_updated for m in markets)
    prov = live(SOURCE, newest, fetched_at=fetched.fetched_at, licence=LICENCE, source_url="https://polymarket.com/", note=NOTE)
    return available(SOURCE, markets, prov)


def parse_history(payload: dict, market_id: str, fetched_at) -> list[domain.PriceObservation]:
    points = []
    for row in (payload or {}).get("history") or []:
        t = _http.epoch(row.get("t"))
        p = _http.number(row.get("p"))
        if t is None or p is None:
            continue
        points.append(domain.PriceObservation(
            series_id=f"market:{market_id}:yes", t=t, value=round(p, 4), unit="probability",
            provenance=live(HISTORY_SOURCE, t, fetched_at=fetched_at, licence=LICENCE, note="daily CLOB price of the Yes outcome; a price, not a probability"),
        ))
    points.sort(key=lambda p: p.t)
    return points


def fetch_market_history(market_id: str) -> SourceResult:
    """Daily Yes-outcome prices for one market (one CLOB request); unavailable when the token is unknown."""
    fetched = _search()
    if isinstance(fetched, _http.Failure):
        return unavailable(HISTORY_SOURCE, fetched.reason)
    _, tokens = parse(fetched.payload, fetched.fetched_at)
    token = tokens.get(market_id)
    if not token:
        return unavailable(HISTORY_SOURCE, "unknown-market")
    history = _http.fetch_json(HISTORY_SOURCE, HISTORY_ENDPOINT, key=f"flopx:polymarket:history:{token[:24]}", ttl_seconds=HISTORY_TTL_SECONDS,
                               params={"market": token, "interval": "max", "fidelity": 1440})
    if isinstance(history, _http.Failure):
        return unavailable(HISTORY_SOURCE, history.reason)
    points = parse_history(history.payload, market_id, history.fetched_at)
    if not points:
        return unavailable(HISTORY_SOURCE, "empty-history")
    return available(HISTORY_SOURCE, points, live(HISTORY_SOURCE, points[-1].t, fetched_at=history.fetched_at, licence=LICENCE))
