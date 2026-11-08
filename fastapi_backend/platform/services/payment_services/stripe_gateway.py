"""Stripe SDK access point.

All payment services obtain the configured SDK module through here so key
handling stays in one place and features degrade cleanly when Stripe is not
configured for an instance.
"""
import socket
import logging

import stripe
import stripe._http_client as stripe_http
import urllib3.util.connection as urllib3_conn

from platform.exceptions import PaymentsNotConfiguredException
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


# Some managed container hosts (observed on Render services) hand the container
# a routable IPv4 address but a black-holed IPv6 route. The Stripe SDK uses
# requests/urllib3, which tries a host's AAAA (IPv6) address first, so calls to
# api.stripe.com stall until the socket timeout while other providers on
# IPv4-first hosts keep working. Forcing urllib3 to resolve IPv4 only avoids the
# dead IPv6 path; IPv4 reaches Stripe everywhere, so it is safe with working IPv6.
urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

# A checkout request must never hang a worker: bound every Stripe call and keep
# retries low so a network stall surfaces as a fast, logged error instead.
stripe.max_network_retries = 1
_STRIPE_TIMEOUT_SECONDS = 20
stripe.default_http_client = stripe_http.RequestsClient(timeout=_STRIPE_TIMEOUT_SECONDS)


def _warm_stripe_resources() -> None:
    """Forces stripe's lazy resource imports once, at single-threaded import time.

    stripe-python imports each resource class on first attribute access (module
    __getattr__). Sync routes and webhooks run in the threadpool, so two requests
    racing that very first import raise "partially initialized module
    'stripe._price'". Touching the classes here, while the app is still importing
    single-threaded, does the imports once and removes the race. Best-effort: a
    class the SDK renames later must not break startup.
    """
    for path in (
        "Price", "Product", "Customer", "Subscription", "PaymentIntent",
        "Charge", "Invoice", "Refund", "File", "WebhookEndpoint", "Webhook",
        "Account", "checkout.Session", "billing_portal.Session",
        "billing_portal.Configuration", "entitlements",
    ):
        obj = stripe
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except Exception:
            pass


_warm_stripe_resources()


def stripe_is_configured() -> bool:
    """True only when payments are FULLY wired: API key AND webhook secret.

    Fail-closed by design. With only the API key, checkout could take money that
    the webhook-driven fulfillment can never record (no entitlement, no purchase
    row) - a half-wired state that is worse than off. So a partial config
    collapses every payment feature to fully dormant, and
    validate_stripe_config() raises the alarm at startup.
    """
    return bool(s.STRIPE_SECRET_KEY and s.STRIPE_WEBHOOK_SECRET)


def validate_stripe_config() -> None:
    """Alerts loudly when Stripe is half-configured (ERROR -> Sentry in prod).

    Called at startup so a missing half can never fail silently: with exactly
    one of the two secrets set, every boot emits a tracked error event until the
    gap is fixed.
    """
    if bool(s.STRIPE_SECRET_KEY) != bool(s.STRIPE_WEBHOOK_SECRET):
        missing = "STRIPE_WEBHOOK_SECRET" if s.STRIPE_SECRET_KEY else "STRIPE_SECRET_KEY"
        logger.error(
            "Stripe is PARTIALLY configured: %s is missing. All payment features are "
            "disabled (fail-closed) until both STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET "
            "are set - a half-wired Stripe could take payments it can never fulfill. "
            "See the stripe-go-live runbook (bootstrap emits the webhook secret).",
            missing,
        )


def get_stripe():
    """Returns the configured Stripe SDK module, raising when payments are disabled"""
    if not stripe_is_configured():
        raise PaymentsNotConfiguredException
    stripe.api_key = s.STRIPE_SECRET_KEY
    return stripe


def get_stripe_for_provisioning():
    """SDK access for provisioning scripts ONLY (scripts/stripe_bootstrap.py).

    The bootstrap is what CREATES the webhook endpoint and emits its secret, so
    it necessarily runs before STRIPE_WEBHOOK_SECRET exists. Runtime services
    must never use this - they go through get_stripe(), which fails closed on a
    partial config.
    """
    if not s.STRIPE_SECRET_KEY:
        raise PaymentsNotConfiguredException
    stripe.api_key = s.STRIPE_SECRET_KEY
    return stripe
