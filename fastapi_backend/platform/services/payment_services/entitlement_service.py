"""Entitlement reads backed by Stripe's Entitlements API.

Stripe computes active entitlements from subscriptions/features; webhooks keep
a per-user cache (`users.entitlements`) fresh so gating checks are a local read.
"""
import logging

from sqlalchemy.orm import Session

from platform.models import User
from platform.services.payment_services.stripe_gateway import get_stripe, stripe_is_configured


logger = logging.getLogger(__name__)


def has_entitlement(user: User, feature: str) -> bool:
    """Checks the cached entitlement list for a feature lookup_key"""
    return feature in (user.entitlements or [])


def get_active_entitlement_keys(customer_id: str) -> list[str]:
    """Reads the customer's active entitlement feature keys from Stripe (no DB writes)"""
    stripe = get_stripe()
    active = stripe.entitlements.ActiveEntitlement.list(customer=customer_id, limit=100)
    return [entitlement.lookup_key for entitlement in active.auto_paging_iter()]


def has_active_subscription(customer_id: str) -> bool:
    """Whether the customer currently has an active subscription (no DB writes).

    Subscription state is strongly consistent in Stripe - a `Subscription.cancel`
    is reflected here immediately - unlike the derived Active Entitlements summary,
    which lags for a propagation window after a cancellation. So this is the
    reliable signal for "should still have access", used to decide whether an
    empty entitlement cache is stale (missed webhook) or genuinely empty (never
    subscribed, or just cancelled).
    """
    stripe = get_stripe()
    subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
    return len(subs.data) > 0


def refresh_entitlements_from_stripe(db: Session, user: User) -> list[str]:
    """Re-reads active entitlements from Stripe and updates the user's cache"""
    if not stripe_is_configured() or not user.stripe_customer_id:
        return list(user.entitlements or [])

    features = get_active_entitlement_keys(user.stripe_customer_id)
    user.entitlements = features
    db.commit()
    return features


def reconcile_all_entitlements(db: Session) -> tuple[int, int]:
    """Reconciles every provider-customer's cached entitlements against the provider.

    The belt-and-suspenders failsafe behind webhooks: webhooks keep the cache
    fresh in real time, but delivery is best-effort, so a low-frequency sweep
    heals any drift a missed event could leave - notably a downgrade
    (subscription cancelled, event lost) that the empty-cache on-read reconcile
    can't catch because the cache is stale-non-empty rather than empty. Only
    users with a provider customer are touched, and a row is written solely when
    its entitlements actually changed (no needless writes / cache churn). Per-user
    failures are logged and skipped so one bad customer never aborts the sweep.

    Returns (customers_scanned, customers_changed).
    """
    if not stripe_is_configured():
        return (0, 0)

    users = db.query(User).filter(User.stripe_customer_id.isnot(None)).all()
    changed = 0
    for user in users:
        try:
            before = sorted(user.entitlements or [])
            after = get_active_entitlement_keys(user.stripe_customer_id)
            if before != sorted(after):
                user.entitlements = after
                db.commit()
                changed += 1
        except Exception:
            db.rollback()
            logger.exception("Entitlement reconcile failed for user %s; skipping.", user.id)
    return (len(users), changed)
