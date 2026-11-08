"""Checkout, billing portal, and purchase-state reads.

Stripe-hosted Checkout is the only purchase surface: the frontend receives a
session URL and redirects. No card data or Stripe.js ever touches this stack.
"""
import logging
import re
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from platform.models import User, Purchase
from platform.enums import LaunchStage, PurchaseStatus
from platform.schemas import CheckoutSessionRequest
from platform.exceptions import (
    CatalogItemNotFoundException,
    PurchaseNotAllowedException,
    PurchaseLimitReachedException,
    BillingProfileNotFoundException,
)
from platform.services.payment_services.catalog import CatalogItem, get_catalog_item
from platform.services.payment_services.stripe_gateway import get_stripe, stripe_is_configured
from platform.services.payment_services.sync_service import ensure_stripe_price
from platform.services.payment_services.entitlement_service import (
    has_active_subscription,
    has_entitlement,
    refresh_entitlements_from_stripe,
)
from platform.services.payment_services.branding_service import get_checkout_branding_settings
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


# Frontend routes for Stripe-hosted redirects. These must match the platform
# checkout routes under next-pwa/app/(platform)/checkout/:
CHECKOUT_SUCCESS_PATH = "/checkout/success?session_id={CHECKOUT_SESSION_ID}"
CHECKOUT_CANCEL_PATH = "/checkout/cancel"
PORTAL_RETURN_PATH = "/dashboard"


def _safe_return_path(return_to: Optional[str]) -> Optional[str]:
    """Accepts only same-origin relative paths, preventing open redirects"""
    if not return_to:
        return None
    if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to:
        return None
    return return_to


def _statement_descriptor_suffix(name: str) -> Optional[str]:
    """Sanitizes a product name into a Stripe statement-descriptor suffix.

    On a shared account the account prefix carries the parent brand, so this
    suffix names the product on the buyer's bank statement (the parent-to-product
    bridge). Stripe requires Latin alphanumerics, no more than 22 characters, and
    forbids `< > \\ " ' *`, so we strip to a safe subset and cap the length. A
    bad value would reject the whole session, so this stays conservative.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 ]", "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:22].strip()


def _support_line() -> Optional[str]:
    """A neutral support-contact line for checkout and invoices.

    Deliberately functional (a support address), not brand voice, so it reads
    well on Stripe's own surfaces. Derived from the instance contact email.
    """
    if not s.CONTACT_EMAIL:
        return None
    return f"Questions about your purchase? Contact {s.CONTACT_EMAIL}."


def _resolve_frontend_origin(request_origin: Optional[str]) -> str:
    """Returns the caller's origin when it is an allowed frontend, else the default.

    One backend can serve several frontends (production, Vercel previews, the
    native shell), so Stripe must redirect back to wherever the request came
    from - validated against the same allow-list CORS uses to avoid open
    redirects - falling back to PUBLIC_FRONTEND_ORIGIN when the origin is absent
    or untrusted.
    """
    if request_origin:
        if request_origin in s.ALLOWED_ORIGINS:
            return request_origin
        regex = s.ALLOWED_ORIGIN_REGEX_COMPUTED
        if regex and re.match(regex, request_origin):
            return request_origin
    return s.PUBLIC_FRONTEND_ORIGIN


def get_or_create_stripe_customer(db: Session, user: User) -> str:
    """Returns the user's Stripe customer id, creating the customer on first use"""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    stripe = get_stripe()
    create_params = {
        "email": user.email,
        "name": user.name,
        "metadata": {"user_id": str(user.id)},
    }
    # A neutral support footer on this customer's invoices (subscription
    # receipts). Invoice logo/colors still come from dashboard branding:
    footer = _support_line()
    if footer:
        create_params["invoice_settings"] = {"footer": footer}

    customer = stripe.Customer.create(**create_params)
    user.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def _quantity_already_purchased(db: Session, user: User, item_key: str) -> int:
    """Sums quantities of this user's pending/paid purchases for an item"""
    total = (
        db.query(func.coalesce(func.sum(Purchase.quantity), 0))
        .filter(
            Purchase.user_id == user.id,
            Purchase.item_key == item_key,
            Purchase.status.in_([PurchaseStatus.PENDING, PurchaseStatus.PAID]),
        )
        .scalar()
    )
    return int(total or 0)


def _validate_purchasable(db: Session, user: User, item: CatalogItem, quantity: int) -> None:
    """Enforces launch stage, purchase window, duplicates, and per-user limits"""
    if item.launch_stage == LaunchStage.HIDDEN:
        raise CatalogItemNotFoundException
    if item.launch_stage == LaunchStage.ADMIN_ONLY and not user.is_admin:
        raise PurchaseNotAllowedException

    if not item.is_purchase_window_open():
        raise PurchaseNotAllowedException("This item is not available for purchase at this time.")

    # Blocking re-subscribing while the entitlement is already active:
    if item.entitlement_feature and has_entitlement(user, item.entitlement_feature):
        raise PurchaseLimitReachedException("You already have an active subscription for this plan.")

    if item.per_user_limit is not None:
        already = _quantity_already_purchased(db, user, item.key)
        if already + quantity > item.per_user_limit:
            raise PurchaseLimitReachedException


def create_checkout_session(db: Session, user: User, data: CheckoutSessionRequest, request_origin: Optional[str] = None) -> str:
    """Creates a Stripe-hosted Checkout session for a catalog item and returns its URL"""
    stripe = get_stripe()

    item = get_catalog_item(data.item_key)
    if item is None:
        raise CatalogItemNotFoundException

    # Subscriptions are single-quantity:
    quantity = 1 if item.interval else data.quantity
    _validate_purchasable(db, user, item, quantity)

    price_id = item.stripe_price_id or ensure_stripe_price(item)
    customer_id = get_or_create_stripe_customer(db, user)

    # Guarding against double-subscribing at the source of truth. The local
    # entitlement cache lags behind payment, so a fast second checkout could
    # otherwise create a duplicate subscription:
    if item.interval:
        # Any non-terminal subscription counts as already-subscribed, not just
        # "active": a plan in trial, past_due, or unpaid must still block a second
        # checkout (status="active" alone would let a trialing user double-subscribe):
        blocking = {"active", "trialing", "past_due", "unpaid"}
        subs = stripe.Subscription.list(customer=customer_id, price=price_id, status="all", limit=100)
        if any(getattr(sub, "status", None) in blocking for sub in subs.data):
            raise PurchaseLimitReachedException("You already have an active subscription for this plan.")

    metadata = {"user_id": str(user.id), "item_key": item.key, "quantity": str(quantity)}
    # Redirecting back to the frontend the buyer actually came from:
    frontend_origin = _resolve_frontend_origin(request_origin)

    # Threading the buyer's origin page through Stripe so the result pages can
    # send them back where they came from:
    success_url = f"{frontend_origin}{CHECKOUT_SUCCESS_PATH}"
    cancel_url = f"{frontend_origin}{CHECKOUT_CANCEL_PATH}"
    return_to = _safe_return_path(data.return_to)
    if return_to:
        encoded = quote(return_to, safe="/")
        success_url += f"&return_to={encoded}"
        cancel_url += f"?return_to={encoded}"

    params = {
        "mode": "subscription" if item.interval else "payment",
        "customer": customer_id,
        "client_reference_id": str(user.id),
        "line_items": [{"price": price_id, "quantity": quantity}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": metadata,
    }

    if item.interval:
        params["subscription_data"] = {"metadata": metadata}
    else:
        # Forcing a Stripe receipt email for one-time payments (sent in live mode
        # regardless of dashboard email settings):
        payment_intent_data = {"receipt_email": user.email}
        # Naming the product on the buyer's bank statement, appended to the
        # account's (parent-brand) descriptor prefix:
        suffix = _statement_descriptor_suffix(item.name)
        if suffix:
            payment_intent_data["statement_descriptor_suffix"] = suffix
        params["payment_intent_data"] = payment_intent_data

    if item.requires_shipping:
        params["shipping_address_collection"] = {"allowed_countries": list(item.shipping_countries)}

    # A neutral support line above the pay button (reinforces trust; not brand
    # voice), shown on the checkout page:
    support_line = _support_line()
    if support_line:
        params["custom_text"] = {"submit": {"message": support_line}}

    # Carrying the product brand onto the hosted checkout page per session, so a
    # shared account still shows each instance's own colors, font, and name.
    # None when disabled/unavailable -> Stripe uses the dashboard branding:
    branding_settings = get_checkout_branding_settings()
    if branding_settings:
        params["branding_settings"] = branding_settings

    session = _create_checkout_session_resilient(stripe, params)
    return session.url


def _create_checkout_session_resilient(stripe, params: dict):
    """Creates the Checkout Session, degrading gracefully if a cosmetic param is rejected.

    Two parameters are cosmetic enhancements that must never block a sale:
    `branding_settings` (per-session brand) and the statement-descriptor suffix
    (the product name on the bank statement). If Stripe rejects either, we drop
    that one and retry, so a schema mismatch or a too-long descriptor can never
    stop the buyer from paying. Any other InvalidRequestError propagates unchanged.
    The loop is bounded by the number of droppable params.
    """
    attempt = dict(params)
    for _ in range(3):
        try:
            return stripe.checkout.Session.create(**attempt)
        except stripe.InvalidRequestError as exc:
            param = str(getattr(exc, "param", "") or "")
            if "branding_settings" in attempt and param.startswith("branding_settings"):
                logger.warning("Stripe rejected branding_settings (%s); retrying without it.", param)
                attempt = {k: v for k, v in attempt.items() if k != "branding_settings"}
                continue
            pid = attempt.get("payment_intent_data") or {}
            if "statement_descriptor" in param and pid.get("statement_descriptor_suffix"):
                logger.warning("Stripe rejected statement descriptor (%s); retrying without it.", param)
                attempt = {**attempt, "payment_intent_data": {k: v for k, v in pid.items() if k != "statement_descriptor_suffix"}}
                continue
            raise
    # All droppable params exhausted; one last attempt so any residual error surfaces:
    return stripe.checkout.Session.create(**attempt)


def create_portal_session(db: Session, user: User, request_origin: Optional[str] = None) -> str:
    """Creates a Stripe Customer Portal session for subscription/billing management"""
    stripe = get_stripe()

    if not user.stripe_customer_id:
        raise BillingProfileNotFoundException

    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{_resolve_frontend_origin(request_origin)}{PORTAL_RETURN_PATH}",
    )
    return session.url


def get_payments_me(db: Session, user: User) -> dict:
    """Returns the user's entitlements and mirrored purchase history.

    Entitlement freshness follows the provider-recommended shape: webhooks are
    the PRIMARY mechanism (they keep `users.entitlements` current), and a
    reconcile is only a FAILSAFE for missed deliveries - not per-request polling,
    which the provider explicitly cautions against on rate-limited read paths.

    So the read stays a plain local-cache read (vendor-neutral, no provider call)
    for anyone who already has entitlements; we reconcile against the provider
    ONLY when the cache is empty AND the provider still reports an active
    subscription. Subscription state is strongly consistent, so it is the
    trustworthy "should have access" signal; the Active Entitlements summary that
    `refresh_entitlements_from_stripe` reads lags for a propagation window after a
    cancellation. Gating on an active subscription recovers the genuine
    stale-empty case (a missed `active_entitlement_summary.updated` event leaving a
    still-subscribed user's cache empty) WITHOUT clobbering an intentional clear:
    right after a cancellation/reset there is no active subscription, so the empty
    cache is left authoritative and the app reflects the loss immediately instead
    of re-reading the still-lagging summary and writing the stale entitlement back.
    Free users have no provider customer, so they never trigger a call. (A periodic
    reconciliation sweep is the complementary failsafe for a downgrade whose event
    was lost while the cache was stale-non-empty - see the payments follow-up note.)
    """
    entitlements = list(user.entitlements or [])
    if (
        not entitlements
        and stripe_is_configured()
        and user.stripe_customer_id
        and has_active_subscription(user.stripe_customer_id)
    ):
        entitlements = list(refresh_entitlements_from_stripe(db, user))
    purchases = (
        db.query(Purchase)
        .filter(Purchase.user_id == user.id)
        .order_by(Purchase.created_at.desc())
        .all()
    )
    return {"entitlements": list(entitlements), "purchases": purchases}


def reset_own_subscription(db: Session, user: User) -> dict:
    """TODO TEMP (QA harness) - remove before launch, with the /payments/test/reset
    route and the pricing page's temp "delete entitlement" button.

    Cancels the caller's own active subscriptions and clears their entitlement cache
    so the pricing CTA flips back to purchasable for re-testing. Safe by construction:
    it only ever touches the calling user's own subscriptions.
    """
    if user.stripe_customer_id:
        stripe = get_stripe()
        active = stripe.Subscription.list(customer=user.stripe_customer_id, status="active", limit=100)
        for sub in active.data:
            stripe.Subscription.cancel(sub.id)
    user.entitlements = []
    db.commit()
    return {"ok": True}
