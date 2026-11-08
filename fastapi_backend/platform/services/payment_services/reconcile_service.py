"""On-demand reconciliation of the local mirror against Stripe.

Webhooks are at-least-once, not guaranteed: deliveries can be missed while the
service is down or fail terminally. This sweep heals the mirror from the
source of truth: it re-imports recent completed Checkout sessions, refreshes
non-terminal purchase statuses from their charges, and re-reads every known
customer's entitlement cache. Idempotent and safe to run at any time.
"""
import json
import logging

from sqlalchemy.orm import Session

from platform.models import User, Purchase
from platform.enums import PurchaseStatus
from platform.services.payment_services.stripe_gateway import get_stripe
from platform.services.payment_services.entitlement_service import get_active_entitlement_keys
from platform.services.payment_services.webhook_service import _handle_checkout_completed


logger = logging.getLogger(__name__)

# Bounds keeping a sweep to a predictable number of Stripe calls:
MAX_SESSIONS = 200
MAX_PURCHASE_CHECKS = 200


def _reimport_recent_sessions(db: Session, stripe, summary: dict) -> None:
    """Upserts mirror rows for recent completed Checkout sessions"""
    sessions = stripe.checkout.Session.list(limit=100, status="complete")
    for session in sessions.auto_paging_iter():
        if summary["sessions_seen"] >= MAX_SESSIONS:
            break
        summary["sessions_seen"] += 1
        try:
            # Per-item savepoint: a failure here (e.g. an IntegrityError from a
            # concurrent live webhook inserting the same session) rolls back only
            # this item, not the whole sweep, so the session stays usable and the
            # final commit still lands the rest:
            with db.begin_nested():
                existed = db.query(Purchase).filter_by(stripe_checkout_session_id=session["id"]).first() is not None
                _handle_checkout_completed(db, session)
            summary["purchases_updated" if existed else "purchases_created"] += 1
        except Exception:
            logger.exception("Reconcile failed for session %s", session.get("id"))


def _refresh_purchase_statuses(db: Session, stripe, summary: dict) -> None:
    """Re-derives refund/dispute state for non-terminal purchases from their charges"""
    purchases = (
        db.query(Purchase)
        .filter(
            Purchase.status.in_([PurchaseStatus.PENDING, PurchaseStatus.PAID]),
            Purchase.stripe_payment_intent_id.isnot(None),
        )
        .order_by(Purchase.created_at.desc())
        .limit(MAX_PURCHASE_CHECKS)
        .all()
    )
    for purchase in purchases:
        try:
            with db.begin_nested():
                intent = json.loads(str(stripe.PaymentIntent.retrieve(purchase.stripe_payment_intent_id, expand=["latest_charge"])))
                charge = intent.get("latest_charge")
                if not charge:
                    continue

                new_status = None
                if charge.get("disputed"):
                    new_status = PurchaseStatus.DISPUTED
                elif charge.get("refunded"):
                    new_status = PurchaseStatus.REFUNDED
                elif (charge.get("amount_refunded") or 0) > 0:
                    new_status = PurchaseStatus.PARTIALLY_REFUNDED
                elif purchase.status == PurchaseStatus.PENDING and intent.get("status") == "succeeded":
                    new_status = PurchaseStatus.PAID

                if new_status and new_status != purchase.status:
                    purchase.status = new_status
                    summary["statuses_corrected"] += 1
        except Exception:
            logger.exception("Reconcile failed for purchase %s", purchase.id)


def _refresh_entitlement_caches(db: Session, summary: dict) -> None:
    """Re-reads active entitlements for every user with a Stripe customer"""
    users = db.query(User).filter(User.stripe_customer_id.isnot(None)).all()
    for user in users:
        try:
            with db.begin_nested():
                user.entitlements = get_active_entitlement_keys(user.stripe_customer_id)
                summary["entitlements_refreshed"] += 1
        except Exception:
            logger.exception("Entitlement refresh failed for user %s", user.id)


def reconcile_with_stripe(db: Session) -> dict:
    """Runs the full sweep and returns per-phase counts"""
    stripe = get_stripe()
    summary = {
        "sessions_seen": 0,
        "purchases_created": 0,
        "purchases_updated": 0,
        "statuses_corrected": 0,
        "entitlements_refreshed": 0,
    }

    _reimport_recent_sessions(db, stripe, summary)
    _refresh_purchase_statuses(db, stripe, summary)
    _refresh_entitlement_caches(db, summary)

    db.commit()
    logger.info("Stripe reconcile complete: %s", summary)
    return summary
