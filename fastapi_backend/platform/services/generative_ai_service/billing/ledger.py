"""
Spend ledger — append-only money records (Phase 4).

Session-first service functions per repo rules. The ledger is the TRUTH for
"what did we spend": budgets are enforced by summing it, and analytics read
it. Rows are never updated or deleted (an audit trail, like the transcript).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AISpendEvent
from ..transcript.types import Usage
from .pricing import PriceResult


def record_spend(
    db: Session,
    *,
    owner_id: str,
    model_id: str,
    usage: Usage,
    price: PriceResult,
    kind: str = "run",
    conversation_id: UUID | None = None,
    run_id: UUID | None = None,
    agent_name: str | None = None,
    created_at: datetime | None = None,
) -> AISpendEvent:
    """
    Appending one spend event. `created_at` is injectable for deterministic
    tests; production leaves it None (server default now()):
    """
    event = AISpendEvent(
        owner_id=owner_id,
        conversation_id=conversation_id,
        run_id=run_id,
        agent_name=agent_name,
        model_id=model_id,
        kind=kind,
        usage=usage.model_dump(mode="json"),
        cost_microusd=price.cost_microusd,
        priced=price.priced,
    )
    if created_at is not None:
        event.created_at = created_at
    try:
        db.add(event)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(event)
    return event


def window_start(window: str, now: datetime) -> datetime | None:
    """
    Computing the UTC start of a budget window: daily = this calendar day,
    weekly = this ISO week (Monday), monthly = this calendar month,
    total = None (all time):
    """
    now = now.astimezone(timezone.utc)
    if window == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "weekly":
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if window == "total":
        return None
    raise ValueError(f"Unknown budget window: {window!r}")


def window_spend(
    db: Session,
    *,
    scope_type: str,
    scope_id: str,
    window: str,
    now: datetime | None = None,
) -> int:
    """
    Summing ledger spend (micro-USD) for a scope over a window. Unpriced rows
    carry their pessimistic cost, so they count too. `now` is injectable for
    deterministic tests:
    """
    now = now or datetime.now(timezone.utc)
    query = select(func.coalesce(func.sum(AISpendEvent.cost_microusd), 0))

    # Scoping the sum to the budget's subject:
    if scope_type == "owner":
        query = query.where(AISpendEvent.owner_id == scope_id)
    elif scope_type == "conversation":
        query = query.where(AISpendEvent.conversation_id == UUID(scope_id))
    elif scope_type == "agent":
        query = query.where(AISpendEvent.agent_name == scope_id)
    elif scope_type == "global":
        pass  # No subject filter — everything counts:
    else:
        raise ValueError(f"Unknown budget scope: {scope_type!r}")

    start = window_start(window, now)
    if start is not None:
        query = query.where(AISpendEvent.created_at >= start)

    return int(db.execute(query).scalar_one())


def usage_summary(db: Session, owner_id: str, now: datetime | None = None) -> dict:
    """
    Building the owner-facing usage report: spend today / this month / total
    (micro-USD converted to USD floats at this boundary) plus a per-model
    breakdown for the month:
    """
    now = now or datetime.now(timezone.utc)

    def _usd(micro: int) -> float:
        return round(micro / 1_000_000, 6)

    month_start = window_start("monthly", now)
    per_model = db.execute(
        select(AISpendEvent.model_id, func.coalesce(func.sum(AISpendEvent.cost_microusd), 0))
        .where(AISpendEvent.owner_id == owner_id, AISpendEvent.created_at >= month_start)
        .group_by(AISpendEvent.model_id)
        .order_by(func.sum(AISpendEvent.cost_microusd).desc())
    ).all()

    return {
        "today_usd": _usd(window_spend(db, scope_type="owner", scope_id=owner_id, window="daily", now=now)),
        "month_usd": _usd(window_spend(db, scope_type="owner", scope_id=owner_id, window="monthly", now=now)),
        "total_usd": _usd(window_spend(db, scope_type="owner", scope_id=owner_id, window="total", now=now)),
        "month_by_model": {model: _usd(micro) for model, micro in per_model},
    }
