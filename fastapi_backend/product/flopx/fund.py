"""Fund analytics for the signed-in paper account (architecture §10.8), derived from fills and closes.

There is no equity snapshot table. The equity curve is rebuilt from the
account's filled orders and the daily closes known on each day, so it is a
`derived` series carrying its inputs' kinds (a curve priced on sample closes
says sample). Alongside: holdings with exposure by ticker and by cluster
(`clusters.json`, an authored editorial grouping), realised and unrealised
P&L, performance since inception, the drawdown series and its maximum,
turnover, an equal-weight benchmark of the eight from the first fill, and
attribution by origin (manual against each strategy) and by signal kind.
Target positions are the strategies' latest PROPOSED decisions and are kept
apart from holdings: a proposal is not a position.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from infrastructure.cache import cache
from product import schemas
from product.exceptions import SampleDataInvalidError
from product.flopx import domain, strategies
from product.flopx.data.loader import SAMPLE_DIR
from product.flopx.paper import broker
from product.flopx.provenance import Provenance, derived, effective_kind
from product.flopx.provenance import sample as sample_provenance
from product.flopx.series import point_in_time

CLUSTERS_FILE = "clusters.json"
CLUSTERS_CACHE_KEY = "flopx:clusters:v1"
CLUSTERS_TTL_SECONDS = 3600
CURVE_MAX_DAYS = 400
REPLAY_KIND = "replay"                  # the signal-kind attribution group for fills placed from a replayed decision


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    tickers: list[str]
    note: str | None = None


def load_clusters_uncached(sample_dir: Path = SAMPLE_DIR) -> tuple[Provenance, list[Cluster]]:
    path = sample_dir / CLUSTERS_FILE
    if not path.exists():
        raise SampleDataInvalidError(file=CLUSTERS_FILE, problem="missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = schemas.SampleFile.model_validate(raw)
        items = [schemas.ClusterSpec.model_validate(item) for item in envelope.items]
    except (ValueError, ValidationError) as exc:
        raise SampleDataInvalidError(file=CLUSTERS_FILE, problem=str(exc)[:500]) from exc
    prov = Provenance.from_dict(envelope.provenance.model_dump(by_alias=False))
    return prov, [Cluster(id=c.id, name=c.name, tickers=list(c.tickers), note=c.note) for c in items]


def load_clusters() -> tuple[Provenance, list[Cluster]]:
    return cache.get_or_set(CLUSTERS_CACHE_KEY, load_clusters_uncached, ttl_seconds=CLUSTERS_TTL_SECONDS, tags=None)


def cluster_of(clusters: list[Cluster]) -> dict[str, str]:
    return {t: c.id for c in clusters for t in c.tickers}


# -- the derived equity curve -------------------------------------------------------------------

def _close_on(series: dict[str, domain.Series], ticker: str, day_end: datetime) -> domain.PriceObservation | None:
    s = series.get(f"equity:{ticker}:close")
    if s is None:
        return None
    known = point_in_time(s, day_end)
    return known[-1] if known else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def equity_curve(fills: list, series: dict[str, domain.Series], as_of: datetime, initial_cash_usd: float) -> tuple[list[dict], list[Provenance]]:
    """Daily account value from the first fill to `as_of`: cash after the fills on or before each day plus
    positions marked at the close known that day (the fill price when no close is known yet)."""
    if not fills:
        return [], []
    fills = sorted(fills, key=lambda o: _aware(o.filled_at))
    first_day = _aware(fills[0].filled_at).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    days = min((last_day - first_day).days, CURVE_MAX_DAYS)
    curve: list[dict] = []
    provs: list[Provenance] = []
    for d in range(days + 1):
        day_end = first_day + timedelta(days=d, hours=23, minutes=59, seconds=59)
        cash = Decimal(str(initial_cash_usd))
        positions: dict[str, Decimal] = {}
        last_fill_price: dict[str, Decimal] = {}
        for o in fills:
            if _aware(o.filled_at) > day_end:
                break
            qty, price = Decimal(str(o.quantity)), Decimal(str(o.fill_price))
            side = o.side.value if hasattr(o.side, "value") else o.side
            if side == "buy":
                cash -= qty * price
                positions[o.ticker] = positions.get(o.ticker, Decimal(0)) + qty
            else:
                cash += qty * price
                positions[o.ticker] = positions.get(o.ticker, Decimal(0)) - qty
            last_fill_price[o.ticker] = price
        value = cash
        kinds: set[str] = set()
        for ticker, qty in positions.items():
            if qty <= 0:
                continue
            close = _close_on(series, ticker, day_end)
            if close is not None:
                value += qty * Decimal(str(close.value))
                kinds.add(effective_kind(close.provenance))
                if d == days:
                    provs.append(close.provenance)
            else:
                value += qty * last_fill_price[ticker]
                kinds.add("fill-price")
        curve.append({"t": day_end.replace(hour=0, minute=0, second=0), "value_usd": float(value.quantize(Decimal("0.01"))), "cash_usd": float(cash.quantize(Decimal("0.01"))),
                      "input_kinds": sorted(kinds)})
    return curve, provs


def drawdowns(curve: list[dict], initial_usd: float, current_equity_usd: float | None = None) -> tuple[list[dict], float]:
    """The drawdown series and its maximum: THE one drawdown computation, used by the fund and
    the desk alike so the two screens cannot disagree (falsifier V3).

    The running peak starts at the account's starting cash, not at the first curve point, so the
    slippage paid on the first fill counts as the drawdown it is; and the account's current
    equity (marked at the latest price, which can be newer than the last daily close) is the
    final point of the path the maximum is taken over."""
    peak = initial_usd
    rows = []
    worst = 0.0
    for point in curve:
        value = point["value_usd"]
        peak = max(peak, value)
        dd = (peak - value) / peak * 100.0 if peak else 0.0
        worst = max(worst, dd)
        rows.append({"t": point["t"], "drawdown_pct": round(dd, 4)})
    if current_equity_usd is not None and peak:
        worst = max(worst, (peak - current_equity_usd) / peak * 100.0)
    return rows, round(max(worst, 0.0), 4)


def account_max_drawdown_pct(db, account, series: dict[str, domain.Series], as_of: datetime, current_equity_usd: float) -> float:
    """The account's maximum drawdown since it opened, for callers that do not need the whole fund
    summary (the desk's account read). Same curve, same `drawdowns()`."""
    orders = broker.list_orders(db, account, limit=10_000)
    fills = [o for o in orders if (o.status.value if hasattr(o.status, "value") else o.status) == "filled" and o.filled_at is not None]
    initial = broker.usd(broker.INITIAL_CASH_MINOR)
    curve, _ = equity_curve(fills, series, as_of, initial)
    return drawdowns(curve, initial, current_equity_usd)[1]


def benchmark_curve(series: dict[str, domain.Series], tickers: list[str], curve: list[dict], initial: float) -> dict:
    """Equal-weight buy-and-hold of `tickers` from the curve's first day, priced on the same closes."""
    if not curve:
        return {"tickers": tickers, "curve": [], "total_return_pct": None, "rule": "equal-weight buy-and-hold from the first fill, no costs"}
    start = curve[0]["t"] + timedelta(hours=23, minutes=59, seconds=59)
    base = {t: _close_on(series, t, start) for t in tickers}
    base = {t: c.value for t, c in base.items() if c is not None and c.value > 0}
    rows = []
    for point in curve:
        day_end = point["t"] + timedelta(hours=23, minutes=59, seconds=59)
        ratios = [(_close_on(series, t, day_end).value / v) for t, v in base.items() if _close_on(series, t, day_end) is not None]
        rows.append({"t": point["t"], "value_usd": round(initial * statistics.fmean(ratios), 2) if ratios else initial})
    total = (rows[-1]["value_usd"] / initial - 1.0) * 100.0 if rows else None
    return {"tickers": sorted(base), "curve": rows, "total_return_pct": round(total, 4) if total is not None else None,
            "rule": "equal-weight buy-and-hold from the first fill, no costs"}


# -- proposed positions: provenance and stated risk ------------------------------------------------

RISK_CLOSES = 60
TRADING_DAYS_PER_CALENDAR_DAY = 5 / 7


def target_risk(series: dict[str, domain.Series], ticker: str | None, at: datetime, horizon_days: int | None,
                notional_usd: float) -> tuple[dict | None, list[Provenance]]:
    """One standard deviation of the proposed position's value over its horizon, from the closes
    known at the decision time: daily close-to-close log-return volatility over the last
    `RISK_CLOSES` closes, scaled by the square root of the horizon in trading days (5/7 of the
    calendar horizon). A statement of spread, not a stop and not a forecast of loss."""
    if ticker is None or not horizon_days:
        return None, []
    s = series.get(f"equity:{ticker}:close")
    known = point_in_time(s, _aware(at)) if s is not None else []
    window = known[-(RISK_CLOSES + 1):]
    if len(window) < 11:
        return None, [p.provenance for p in window[-1:]]
    returns = [math.log(b.value / a.value) for a, b in zip(window, window[1:]) if a.value > 0 and b.value > 0]
    if len(returns) < 10:
        return None, [window[-1].provenance]
    daily = statistics.stdev(returns)
    days = max(1, round(horizon_days * TRADING_DAYS_PER_CALENDAR_DAY))
    one_sigma = daily * math.sqrt(days)
    return {
        "measure": "one-sigma-move-over-horizon",
        "daily_volatility_pct": round(daily * 100.0, 4),
        "horizon_trading_days": days,
        "one_sigma_pct": round(one_sigma * 100.0, 4),
        "one_sigma_usd": round(notional_usd * one_sigma, 2),
        "notional_usd": round(notional_usd, 2),
        "closes": len(window),
        "rule": f"daily log-return volatility over the last {RISK_CLOSES} closes known at the decision, times the square root of the horizon in trading days",
    }, [p.provenance for p in window]


# -- attribution ----------------------------------------------------------------------------------

def attribution(fills: list, price_lookup: broker.PriceLookup, key_of) -> dict[str, dict]:
    """P&L by the origin of the order that OPENED each lot, first in first out.

    Every buy opens a lot that keeps its opener's key (`key_of(order)`); every sell consumes the
    ticker's oldest lots first, and the realised P&L of each consumed slice (sell fill minus the
    lot's fill, times the quantity) is that lot's group's. Lots still open are marked at the latest
    price for the unrealised part. So a closed round trip has no open value, and a lot a strategy
    opened and a person closed by hand counts to the strategy. Grouping fills by their own origin
    (the earlier design) booked a manual close of a strategy lot as manual profit equal to its
    whole notional (falsifier V4 F9)."""
    lots: dict[str, list[list]] = {}
    groups: dict[str, dict] = {}

    def group(key: str) -> dict:
        return groups.setdefault(key, {"fills": 0, "bought": Decimal(0), "sold": Decimal(0), "realised": Decimal(0), "open_value": Decimal(0),
                                       "unrealised": Decimal(0), "unpriced": []})

    for o in sorted(fills, key=lambda o: _aware(o.filled_at)):
        qty, price = Decimal(str(o.quantity)), Decimal(str(o.fill_price))
        side = o.side.value if hasattr(o.side, "value") else o.side
        if side == "buy":
            key = key_of(o)
            lots.setdefault(o.ticker, []).append([qty, price, key])
            g = group(key)
            g["fills"] += 1
            g["bought"] += qty * price
            continue
        remaining, touched = qty, []
        queue = lots.get(o.ticker, [])
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(remaining, lot[0])
            g = group(lot[2])
            g["sold"] += take * price
            g["realised"] += take * (price - lot[1])
            if lot[2] not in touched:
                touched.append(lot[2])
            lot[0] -= take
            remaining -= take
            if lot[0] <= 0:
                queue.pop(0)
        for key in touched:
            group(key)["fills"] += 1

    for ticker, queue in lots.items():
        open_lots = [lot for lot in queue if lot[0] > 0]
        if not open_lots:
            continue
        last = price_lookup(ticker)
        for qty, cost, key in open_lots:
            g = group(key)
            if last is None:
                if ticker not in g["unpriced"]:
                    g["unpriced"].append(ticker)
                continue
            mark = Decimal(str(last.value))
            g["open_value"] += qty * mark
            g["unrealised"] += qty * (mark - cost)

    def cents(value: Decimal) -> float:
        return float(value.quantize(Decimal("0.01")))

    return {key: {"fills": g["fills"], "bought_usd": cents(g["bought"]), "sold_usd": cents(g["sold"]), "open_value_usd": cents(g["open_value"]),
                  "realised_pnl_usd": cents(g["realised"]), "unrealised_pnl_usd": cents(g["unrealised"]),
                  "pnl_usd": cents(g["realised"] + g["unrealised"]), "unpriced": sorted(g["unpriced"])}
            for key, g in sorted(groups.items())}


def summary(
    db,
    account,
    *,
    series: dict[str, domain.Series],
    price_lookup: broker.PriceLookup,
    clusters: list[Cluster],
    clusters_provenance: Provenance,
    decisions: list,
    tickers: list[str],
    as_of: datetime,
) -> dict:
    acct = broker.summary(db, account, price_lookup)
    orders = broker.list_orders(db, account, limit=10_000)
    fills = [o for o in orders if (o.status.value if hasattr(o.status, "value") else o.status) == "filled" and o.filled_at is not None]
    initial = broker.usd(broker.INITIAL_CASH_MINOR)
    curve, curve_provs = equity_curve(fills, series, as_of, initial)
    dd_series, max_dd = drawdowns(curve, initial, acct.equity_usd)
    bench = benchmark_curve(series, tickers, curve, initial)
    by_cluster_id = cluster_of(clusters)
    exposure_by_cluster: dict[str, float] = {}
    for p in acct.positions:
        if p.weight is not None:
            cid = by_cluster_id.get(p.ticker, "unclustered")
            exposure_by_cluster[cid] = round(exposure_by_cluster.get(cid, 0.0) + p.weight, 4)
    kind_of_order: dict[str, str] = {}
    replay_strategy_of_order: dict[str, str] = {}
    for row in decisions:
        replayed = strategies.is_replay(row)
        for oid in (row.order_ids or []):
            # A replayed decision's fill is hindsight, not forward evidence: it is attributed to its
            # own `replay:<strategy>` group, never to the strategy's forward results (falsifier R2-06).
            # Keyed from the decision, so fills placed before replays had their own origin count right too.
            kind_of_order[oid] = REPLAY_KIND if replayed else (row.signal_kind or "unknown")
            if replayed:
                replay_strategy_of_order[oid] = row.strategy_id
    turnover_usd = sum(float(Decimal(str(o.quantity)) * Decimal(str(o.fill_price))) for o in fills)
    inception = _aware(fills[0].filled_at) if fills else None
    # observedAt is the newest INPUT's time (derived() defaults to it): the curve is only as fresh
    # as the closes and fills it is computed from, never "now" (falsifier V1 F15).
    curve_prov = derived("fund:equity-curve-v1", curve_provs + [Provenance.from_dict(o.fill_provenance) for o in fills if o.fill_provenance],
                         note="rebuilt from filled orders and daily closes; no snapshot table") if fills else None
    # The account value is cash plus positions at their marks: it carries the marks' kinds, so a
    # value marked to a sample close says SAMPLE (falsifier V1 F14). Cash alone needs no mark.
    account_prov = derived("fund:account-value-v1", acct.provenances,
                           note="cash plus positions at their latest marks") if acct.provenances else None
    proposals = []
    for r in decisions:
        if r.status != "proposed":
            continue
        notional = (r.proposed_weight or 0.0) * strategies.ACCOUNT_REFERENCE_USD
        risk, risk_provs = target_risk(series, r.ticker, r.t, r.horizon_days, notional)
        # Every forward decision is computed by the rule over the sample history (step_forward
        # reads SampleData), so its inputs are sample even when no close was found for the risk.
        inputs = risk_provs or [sample_provenance(note="strategy rule inputs")]
        proposals.append({
            "decision_id": r.id, "strategy_id": r.strategy_id, "t": r.t, "action": r.action.value if hasattr(r.action, "value") else r.action,
            "ticker": r.ticker, "proposed_weight": r.proposed_weight, "horizon_days": r.horizon_days, "rationale": r.rationale,
            "status": r.status, "signal_id": r.signal_id, "expires_at": strategies.proposal_expires_at(r),
            "uncertainty": {"label": "exploratory", "note": "a rule's proposal over the data known at its time; not executed and not a position"},
            "provenance": derived("fund:target-position-v1", inputs, note="the rule's proposal and its stated risk, from the closes known at the decision"),
            "risk": risk,
            "statement_class": "decision"})
    return {
        "paper": True,
        "account": {**{k: v for k, v in acct.__dict__.items() if k != "provenances"}, "max_drawdown_pct": max_dd},
        "account_provenance": account_prov,
        "exposure_by_cluster": exposure_by_cluster,
        "clusters": [c.__dict__ for c in clusters],
        "clusters_provenance": clusters_provenance,
        "inception": inception,
        "performance_since_inception_pct": round((acct.equity_usd / initial - 1.0) * 100.0, 4),
        "equity_curve": curve,
        "equity_curve_provenance": curve_prov,
        "drawdown_series": dd_series,
        "max_drawdown_pct": max_dd,
        "turnover_usd": round(turnover_usd, 2),
        "turnover_ratio": round(turnover_usd / initial, 4),
        "benchmark": bench,
        "attribution_by_origin": attribution(fills, price_lookup, lambda o: strategies.order_origin(replay_strategy_of_order[str(o.id)], True)
                                             if str(o.id) in replay_strategy_of_order else o.origin),
        "attribution_by_signal_kind": attribution(fills, price_lookup, lambda o: kind_of_order.get(str(o.id), "manual" if o.origin == "manual" else "unlinked")),
        "target_positions": proposals,
        "fills": len(fills),
        "provenances": list(acct.provenances) + curve_provs,
    }
