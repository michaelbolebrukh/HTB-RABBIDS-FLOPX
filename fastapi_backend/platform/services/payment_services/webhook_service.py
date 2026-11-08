"""Stripe webhook processing.

The webhook is the only writer of purchase/entitlement state: refunds issued
from the Stripe dashboard (e.g. resolving an email complaint) and subscription
lifecycle changes reconcile back into the local mirror automatically.
Idempotency is enforced through the stripe_events table.
"""
import json
import logging

import stripe as stripe_lib
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from platform.models import User, Purchase, StripeEvent
from platform.enums import PurchaseStatus, ProductType
from platform.exceptions import InvalidWebhookException, PaymentsNotConfiguredException
from platform.services.payment_services.catalog import get_catalog_item
from platform.services.payment_services.stripe_gateway import get_stripe
from platform.services.payment_services.entitlement_service import get_active_entitlement_keys
from platform.services.payment_services.notification_service import (
    notify_admin_order,
    notify_admin_dispute,
    send_order_confirmation,
    send_subscription_welcome,
    send_subscription_renewal,
    send_refund_confirmation,
)
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


def _as_dict(stripe_object) -> dict:
    """Converts a Stripe SDK object to a plain dict (StripeObjects lack dict .get)"""
    if isinstance(stripe_object, dict):
        return stripe_object
    return json.loads(str(stripe_object))


def _get_user_by_customer(db: Session, customer_id: str | None) -> User | None:
    if not customer_id:
        return None
    return db.query(User).filter_by(stripe_customer_id=customer_id).first()


# ============================================================================
# EVENT HANDLERS
# ============================================================================

def _handle_checkout_completed(db: Session, obj: dict) -> None:
    """Mirrors a completed Checkout session as a Purchase row"""
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id")
    item_key = metadata.get("item_key")
    if not user_id or not item_key:
        logger.warning("Checkout session %s missing metadata; skipping", obj.get("id"))
        return

    # Instance scoping on a shared Stripe account: Stripe delivers every account
    # event to every registered endpoint, so an event whose item_key is not in
    # THIS instance's catalog belongs to another instance sharing the account.
    # Skipping it avoids a spurious Purchase and, critically, avoids clobbering a
    # local user's stripe_customer_id via the user_id collision across databases:
    item = get_catalog_item(item_key)
    if item is None:
        logger.info("Checkout session %s for unknown item_key %r; not this instance, skipping", obj.get("id"), item_key)
        return
    item_type = item.type.value

    # user_id is authored by us, but guarding the cast so a malformed value can't
    # become a poison event that Stripe retries forever:
    try:
        user_pk = int(user_id)
    except (TypeError, ValueError):
        logger.warning("Checkout session %s has non-numeric user_id %r; skipping", obj.get("id"), user_id)
        return

    # Upserting by session id (Stripe may retry deliveries):
    existing = db.query(Purchase).filter_by(stripe_checkout_session_id=obj["id"]).first()
    is_new = existing is None

    paid = obj.get("payment_status") == "paid"
    status = PurchaseStatus.PAID if paid else PurchaseStatus.PENDING

    # Shipping details location moved across Checkout API versions:
    shipping = (obj.get("collected_information") or {}).get("shipping_details") or obj.get("shipping_details")

    # Quantity travels in session metadata (set at checkout creation), avoiding an
    # extra Stripe API round-trip inside the webhook request:
    try:
        quantity = int(metadata.get("quantity") or 1)
    except (TypeError, ValueError):
        quantity = 1

    if existing:
        # Only advancing PENDING; never regressing terminal states (refunded,
        # disputed) on redelivery or reconcile re-import:
        if existing.status == PurchaseStatus.PENDING:
            existing.status = status
        existing.shipping = shipping or existing.shipping
    else:
        db.add(Purchase(
            user_id=user_pk,
            item_key=item_key,
            item_type=item_type,
            stripe_checkout_session_id=obj["id"],
            stripe_payment_intent_id=obj.get("payment_intent"),
            stripe_subscription_id=obj.get("subscription"),
            amount_total_minor=obj.get("amount_total") or 0,
            currency=(obj.get("currency") or "gbp").upper(),
            quantity=quantity,
            shipping=shipping,
            status=status,
        ))

    # Backfilling the customer link in case the user record predates Checkout:
    user = db.query(User).filter_by(id=user_pk).first()
    if user and not user.stripe_customer_id and obj.get("customer"):
        user.stripe_customer_id = obj["customer"]

    # On a genuinely new paid order (deduped by event id, so redeliveries do not
    # re-notify): confirm to the buyer and alert the operator.
    if paid and is_new:
        amount_minor = obj.get("amount_total") or 0
        currency = obj.get("currency") or "gbp"

        # Customer-facing email to the buyer (a subscription gets a welcome, a
        # one-time purchase gets an order confirmation). Best-effort: skips
        # silently if the user row or its email is unavailable:
        if user and user.email:
            if item_type == ProductType.SUBSCRIPTION.value:
                send_subscription_welcome(
                    recipient_email=user.email,
                    recipient_name=user.name,
                    plan_name=item.name,
                    amount_minor=amount_minor,
                    currency=currency,
                    reference=obj.get("id"),
                )
            else:
                send_order_confirmation(
                    recipient_email=user.email,
                    recipient_name=user.name,
                    item_name=item.name,
                    amount_minor=amount_minor,
                    currency=currency,
                    quantity=quantity,
                    reference=obj.get("id"),
                    created=obj.get("created"),
                )

        notify_admin_order(
            item_key=item_key,
            item_type=item_type,
            amount_minor=amount_minor,
            currency=currency,
            quantity=quantity,
            user_id=user_id,
            ships=bool(item and item.requires_shipping),
            session_id=obj.get("id"),
        )


def _handle_async_payment_succeeded(db: Session, obj: dict) -> None:
    purchase = db.query(Purchase).filter_by(stripe_checkout_session_id=obj["id"]).first()
    if purchase:
        purchase.status = PurchaseStatus.PAID


def _handle_async_payment_failed(db: Session, obj: dict) -> None:
    """Releases the pending mirror row so a failed payment doesn't consume per-user limits"""
    purchase = db.query(Purchase).filter_by(stripe_checkout_session_id=obj["id"]).first()
    if purchase and purchase.status == PurchaseStatus.PENDING:
        purchase.status = PurchaseStatus.FAILED


def _handle_entitlement_summary_updated(db: Session, obj: dict) -> None:
    """Refreshes the user's entitlement cache from Stripe's computed summary"""
    user = _get_user_by_customer(db, obj.get("customer"))
    if user is None:
        logger.warning("Entitlement summary for unknown customer %s", obj.get("customer"))
        return

    entitlements = obj.get("entitlements") or {}

    # The embedded list is paginated; refetching the full set when truncated:
    if entitlements.get("has_more"):
        user.entitlements = get_active_entitlement_keys(obj["customer"])
        return

    entitlement_data = entitlements.get("data") or []
    user.entitlements = [e.get("lookup_key") for e in entitlement_data if e.get("lookup_key")]


def _find_purchase_for_charge(db: Session, obj: dict) -> Purchase | None:
    """Locates the mirrored purchase for a charge, via payment intent or invoice subscription"""
    payment_intent_id = obj.get("payment_intent")
    if payment_intent_id:
        purchase = db.query(Purchase).filter_by(stripe_payment_intent_id=payment_intent_id).first()
        if purchase:
            return purchase

    # Subscription charges pay invoices, so the session's payment_intent is null;
    # resolving through the invoice's subscription instead:
    invoice_id = obj.get("invoice")
    if invoice_id:
        try:
            invoice = _as_dict(get_stripe().Invoice.retrieve(invoice_id))
            subscription_id = (
                invoice.get("subscription")
                or ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
            )
            if subscription_id:
                return db.query(Purchase).filter_by(stripe_subscription_id=subscription_id).first()
        except Exception:
            logger.exception("Failed to resolve invoice %s for charge %s", invoice_id, obj.get("id"))

    return None


def _handle_charge_refunded(db: Session, obj: dict) -> None:
    """Reconciles dashboard-issued refunds into the purchase mirror"""
    purchase = _find_purchase_for_charge(db, obj)
    if purchase is None:
        logger.warning("Refund for unmatched charge %s", obj.get("id"))
        return

    # Not regressing a more urgent terminal state: a refund on an already-disputed
    # purchase keeps DISPUTED, since the operator still owes a dispute response:
    if purchase.status == PurchaseStatus.DISPUTED:
        logger.info("Refund on disputed purchase %s; keeping DISPUTED", purchase.id)
        return

    purchase.status = PurchaseStatus.REFUNDED if obj.get("refunded") else PurchaseStatus.PARTIALLY_REFUNDED

    # Instance-branded refund receipt to the buyer (deduped by event id, so each
    # distinct refund event sends exactly one email). Best-effort:
    user = db.query(User).filter_by(id=purchase.user_id).first()
    item = get_catalog_item(purchase.item_key)
    if user and user.email:
        refunds = (obj.get("refunds") or {}).get("data") or []
        refund_created = refunds[0].get("created") if refunds else None
        send_refund_confirmation(
            recipient_email=user.email,
            recipient_name=user.name,
            item_name=item.name if item else purchase.item_key,
            amount_minor=obj.get("amount_refunded") or 0,
            currency=obj.get("currency") or purchase.currency,
            reference=purchase.stripe_checkout_session_id or obj.get("id"),
            created=refund_created,
        )


def _handle_invoice_paid(db: Session, obj: dict) -> None:
    """Sends an instance-branded renewal receipt for a recurring subscription charge.

    invoice.paid fires for the initial subscription invoice too, so this only acts
    on `subscription_cycle` (the recurring renewal) - the initial invoice is already
    covered by the subscription-welcome email from checkout.session.completed.
    """
    if obj.get("billing_reason") != "subscription_cycle":
        return

    # Subscription id moved under `parent` in newer API versions:
    subscription_id = (
        obj.get("subscription")
        or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    )
    if not subscription_id:
        return

    # Instance scoping on a shared account: only a subscription this instance
    # mirrored has a local Purchase row; others belong to sibling instances:
    purchase = db.query(Purchase).filter_by(stripe_subscription_id=subscription_id).first()
    if purchase is None:
        return

    item = get_catalog_item(purchase.item_key)
    user = db.query(User).filter_by(id=purchase.user_id).first()
    if not (user and user.email and item):
        return

    send_subscription_renewal(
        recipient_email=user.email,
        recipient_name=user.name,
        plan_name=item.name,
        amount_minor=obj.get("amount_paid") or 0,
        currency=obj.get("currency") or purchase.currency,
        reference=obj.get("number") or obj.get("id"),
        created=obj.get("created"),
    )


def _handle_dispute_created(db: Session, obj: dict) -> None:
    """Marks the disputed purchase; Stripe also emails the account owner directly"""
    purchase = None
    payment_intent_id = obj.get("payment_intent")
    if payment_intent_id:
        purchase = db.query(Purchase).filter_by(stripe_payment_intent_id=payment_intent_id).first()

    if purchase is None and obj.get("charge"):
        try:
            charge = _as_dict(get_stripe().Charge.retrieve(obj["charge"]))
            purchase = _find_purchase_for_charge(db, charge)
        except Exception:
            logger.exception("Failed to resolve charge %s for dispute", obj.get("charge"))

    if purchase is None:
        logger.warning("Dispute for unmatched charge %s", obj.get("charge"))
    elif purchase.status in (PurchaseStatus.REFUNDED, PurchaseStatus.PARTIALLY_REFUNDED):
        logger.info("Dispute on already-refunded purchase %s; keeping refunded status", purchase.id)
    else:
        purchase.status = PurchaseStatus.DISPUTED

    # Alerting the operator either way - a dispute needs a timely response even if
    # the local mirror could not be matched:
    notify_admin_dispute(
        charge_id=obj.get("charge"),
        amount_minor=obj.get("amount") or 0,
        currency=obj.get("currency") or "gbp",
        reason=obj.get("reason"),
        item_key=purchase.item_key if purchase else None,
        user_id=str(purchase.user_id) if purchase else None,
    )


def _handle_subscription_deleted(db: Session, obj: dict) -> None:
    """Safety net: re-reads entitlements when a subscription ends.

    Failures propagate so the event is not marked processed and Stripe retries.
    """
    user = _get_user_by_customer(db, obj.get("customer"))
    if user is None:
        return
    user.entitlements = get_active_entitlement_keys(user.stripe_customer_id)


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "checkout.session.async_payment_succeeded": _handle_async_payment_succeeded,
    "checkout.session.async_payment_failed": _handle_async_payment_failed,
    "entitlements.active_entitlement_summary.updated": _handle_entitlement_summary_updated,
    "invoice.paid": _handle_invoice_paid,
    "charge.refunded": _handle_charge_refunded,
    "charge.dispute.created": _handle_dispute_created,
    "customer.subscription.deleted": _handle_subscription_deleted,
}

# Canonical set of Stripe events this backend subscribes to: exactly the events it
# has a handler for. The webhook bootstrap (scripts/stripe_bootstrap.py) and the
# stripe-ops skill mirror this list; deriving it from _HANDLERS keeps them from
# drifting (a handler added without subscribing means the event is never delivered):
SUBSCRIBED_WEBHOOK_EVENTS = tuple(_HANDLERS)


# ============================================================================
# ENTRY POINT
# ============================================================================

def handle_webhook(db: Session, payload: bytes, signature: str | None) -> None:
    """Verifies, deduplicates, and dispatches a Stripe webhook event"""
    if not s.STRIPE_WEBHOOK_SECRET:
        raise PaymentsNotConfiguredException

    try:
        # Verifying the signature; the return value is only used to confirm authenticity:
        stripe_lib.Webhook.construct_event(payload, signature, s.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise InvalidWebhookException

    # Parsing the verified payload as plain JSON so handlers receive dicts (the
    # SDK Event exposes StripeObjects without dict .get, which the handlers use):
    event = json.loads(payload)
    event_id = event["id"]
    event_type = event["type"]

    # Skipping already-processed events (Stripe retries deliveries):
    if db.query(StripeEvent).filter_by(id=event_id).first():
        return

    handler = _HANDLERS.get(event_type)

    try:
        if handler:
            handler(db, event["data"]["object"])
        db.add(StripeEvent(id=event_id, type=event_type))
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the insert race; treating
        # this delivery as already processed:
        db.rollback()
    except Exception:
        db.rollback()
        raise
