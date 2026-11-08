"""Signals and their chains (architecture §10.7): signal -> forecast -> decision -> orders -> outcome.

A signal is a dated, sourced change the system considered actionable. Two
producers exist: the strategies' `decide()` (every buy or sell decision names
its signal, `signal:<strategy>:<ticker>:<date>`) and the anomaly detector (one
signal per anomaly, `signal:anomaly:<anomaly id>`). Signals are not stored: a
strategy signal is reconstructed from the decision rows that reference it, an
anomaly signal from the current anomaly, so a signal can never contradict its
decisions. A signal with no decision carries `skipped: {reason}`:
`no-documented-exposure`, `insufficient-sample`, `risk-limit`,
`strategy-not-followed`.

The forecast in the chain is the evaluation harness's baseline for the
connected instrument at the signal's date (the horizon the strategy trades),
computed from closes known then; the outcome is filled in once the horizon has
passed, from closes observed later, and is absent until then.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from product.flopx import domain
from product.flopx.anomalies import authored_basis_text
from product.flopx.evaluation import baseline_forecast
from product.flopx.provenance import Provenance, derived
from product.flopx.series import point_in_time

ANOMALY_HORIZON_DAYS = 21
REASON_NO_EXPOSURE = "no-documented-exposure"
REASON_INSUFFICIENT = "insufficient-sample"
REASON_RISK = "risk-limit"
REASON_NOT_FOLLOWED = "strategy-not-followed"


@dataclass(frozen=True)
class DecisionRef:
    decision_id: int | None
    strategy_id: str
    t: datetime
    action: str
    ticker: str | None
    status: str
    rationale: dict
    proposed_weight: float | None
    horizon_days: int | None
    order_ids: list[str]
    skipped_reason: str | None
    mode: str
    statement_class: str = "decision"


@dataclass(frozen=True)
class OrderRef:
    id: str
    status: str
    side: str
    quantity: float
    fill_price: float | None
    filled_at: datetime | None
    origin: str


@dataclass(frozen=True)
class Signal:
    id: str
    at: datetime
    kind: str
    source_series_id: str | None
    source_id: str | None
    magnitude: float | None
    evidence: list[Provenance]
    connected: list[dict]
    forecast_ref: dict | None
    decision_refs: list[DecisionRef]
    orders: list[OrderRef]
    outcome: dict | None
    skipped: dict | None
    provenance: Provenance
    headline: str = ""
    statement_class: str = "inference"


def _aware(value: datetime) -> datetime:
    """Rows read back from SQLite lose their offset; everything here compares in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _closes_known(series: dict[str, domain.Series], ticker: str, at: datetime) -> list[domain.PriceObservation]:
    s = series.get(f"equity:{ticker}:close")
    return point_in_time(s, at) if s else []


def forecast_for(series: dict[str, domain.Series], ticker: str, at: datetime, horizon_days: int) -> dict | None:
    known = _closes_known(series, ticker, at)
    b = baseline_forecast(known, horizon_days)
    if b is None:
        return None
    return {**b, "ticker": ticker, "at": at, "known_closes": len(known), "statement_class": "forecast",
            "exploratory": True, "basis": "trailing-mean drift of daily log returns from closes known at the signal date; no skill is claimed"}


def outcome_for(series: dict[str, domain.Series], ticker: str, at: datetime, horizon_days: int, forecast: dict | None) -> dict | None:
    """The realised log return over the horizon, from a close observed at or after at + horizon; None until one exists."""
    s = series.get(f"equity:{ticker}:close")
    if s is None:
        return None
    start = [p for p in s.points if p.t <= at]
    later = [p for p in s.points if p.t >= at + timedelta(days=horizon_days)]
    if not start or not later or start[-1].value <= 0 or later[0].value <= 0:
        return None
    realised = math.log(later[0].value / start[-1].value)
    out = {"horizon_days": horizon_days, "realised_return": round(realised, 6), "evaluated_at": later[0].t, "ticker": ticker,
           "from_close": start[-1].value, "to_close": later[0].value, "input_kinds": sorted({start[-1].provenance.kind, later[0].provenance.kind})}
    if forecast is not None:
        out["forecast_error"] = round(realised - forecast["expected_log_return"], 6)
        out["within_band"] = forecast["lower"] <= realised <= forecast["upper"]
    return out


def _order_ref(order) -> OrderRef:
    status = order.status.value if hasattr(order.status, "value") else order.status
    side = order.side.value if hasattr(order.side, "value") else order.side
    return OrderRef(id=str(order.id), status=status, side=side, quantity=float(order.quantity or 0),
                    fill_price=float(order.fill_price) if order.fill_price is not None else None, filled_at=order.filled_at, origin=order.origin)


def _decision_ref(row) -> DecisionRef:
    return DecisionRef(
        decision_id=row.id, strategy_id=row.strategy_id, t=_aware(row.t), action=row.action.value if hasattr(row.action, "value") else row.action,
        ticker=row.ticker, status=row.status, rationale=row.rationale or {}, proposed_weight=row.proposed_weight, horizon_days=row.horizon_days,
        order_ids=list(row.order_ids or []), skipped_reason=row.skipped_reason, mode=row.mode.value if hasattr(row.mode, "value") else row.mode,
    )


def from_decisions(rows: list, series: dict[str, domain.Series], orders_by_id: dict[str, object], *, strategies: dict) -> list[Signal]:
    """One signal per distinct signal id across the decision rows that carry one."""
    grouped: dict[str, list] = {}
    for row in rows:
        if row.signal_id:
            grouped.setdefault(row.signal_id, []).append(row)
    out: list[Signal] = []
    for signal_id, group in grouped.items():
        group.sort(key=lambda r: (_aware(r.t), r.id or 0))
        first = group[0]
        first_t = _aware(first.t)
        spec = strategies.get(first.strategy_id)
        rationale = first.rationale or {}
        magnitude = next((rationale[k] for k in ("z", "ratio", "severity", "return3d") if k in rationale), None)
        source_id = rationale.get("itemId")
        source_series_id = None if source_id else (spec.parameters.get("index") if spec else None)
        horizon = first.horizon_days or (spec and 20) or 20
        forecast = forecast_for(series, first.ticker, first_t, horizon) if first.ticker else None
        outcome = outcome_for(series, first.ticker, first_t, horizon, forecast) if first.ticker else None
        orders = [_order_ref(orders_by_id[oid]) for r in group for oid in (r.order_ids or []) if oid in orders_by_id]
        provs = [p.provenance for p in _closes_known(series, first.ticker, first_t)[-1:]] if first.ticker else []
        prov = derived("signal:strategy-decision", provs, observed_at=first_t, note="reconstructed from the decision ledger")
        skipped = None
        if all(r.status == "skipped" for r in group):
            skipped = {"reason": group[-1].skipped_reason or REASON_NOT_FOLLOWED}
        out.append(Signal(
            id=signal_id, at=first_t, kind=first.signal_kind or "price-change", source_series_id=source_series_id, source_id=source_id,
            magnitude=magnitude, evidence=provs, connected=[{"instrument": first.ticker, "basis": f"strategy rule: {spec.rule if spec else first.strategy_id}"}],
            forecast_ref=forecast, decision_refs=[_decision_ref(r) for r in group], orders=orders, outcome=outcome, skipped=skipped, provenance=prov,
            headline=f"{first.strategy_id}: {rationale.get('signal', first.action)} on {first.ticker} ({first.signal_kind})",
        ))
    return out


def no_documented_note(links: list[domain.Exposure]) -> str:
    """Why no company is documented here, naming the basis the listed links actually rest on (R1-8)."""
    if not links:
        return "no company is linked to the cell at all"
    listed = (f"the listed company ({links[0].ticker}) is" if len(links) == 1
              else f"the {len(links)} listed companies ({', '.join(e.ticker for e in links)}) are")
    return f"no company is linked to the cell by an observed facility record or an SEC filing; {listed} {authored_basis_text(links)}"


def from_anomaly(anomaly: domain.Anomaly, series: dict[str, domain.Series], *, decisions: list = ()) -> Signal:
    """One signal per anomaly; skipped unless a decision row references it (none does today, which the reason says)."""
    documented = [e for e in anomaly.linked_companies if e.documented]
    connected = [{"instrument": e.ticker, "basis": ", ".join(e.basis)} for e in documented]
    ticker = documented[0].ticker if documented else None
    forecast = forecast_for(series, ticker, anomaly.first_seen, ANOMALY_HORIZON_DAYS) if ticker else None
    outcome = outcome_for(series, ticker, anomaly.first_seen, ANOMALY_HORIZON_DAYS, forecast) if ticker else None
    refs = [_decision_ref(r) for r in decisions]
    skipped = None
    if not refs:
        if not documented:
            skipped = {"reason": REASON_NO_EXPOSURE, "note": no_documented_note(anomaly.linked_companies)}
        elif anomaly.uncertainty.coverage < 3 or anomaly.uncertainty.label == "exploratory" and anomaly.uncertainty.factors_with_data < 4:
            skipped = {"reason": REASON_INSUFFICIENT, "note": f"coverage {anomaly.uncertainty.coverage} asks, {anomaly.uncertainty.factors_with_data} factors with data"}
        else:
            skipped = {"reason": REASON_NOT_FOLLOWED, "note": "no strategy consumes anomaly signals; the three rules trade the index, its regime and feed events"}
    prov = derived("signal:anomaly", [anomaly.provenance], observed_at=anomaly.first_seen, note=anomaly.rule)
    return Signal(
        id=f"signal:anomaly:{anomaly.id}", at=anomaly.first_seen, kind="anomaly", source_series_id=None, source_id=anomaly.id,
        magnitude=float(anomaly.severity), evidence=[anomaly.provenance], connected=connected, forecast_ref=forecast, decision_refs=refs,
        orders=[], outcome=outcome, skipped=skipped, provenance=prov, headline=anomaly.headline,
    )
